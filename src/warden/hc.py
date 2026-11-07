"""Herdr drivers.

``HcDriver`` wraps the ``hc`` CLI (herdr-core, ``--json`` output): fleet
machines, module terminals, remote exec and file writes. ``HerdrApp`` wraps the
``herdr-app`` CLI to show module terminals in Herdr.app, in a
``warden <machine>`` space on each fleet machine.

Ownership guard: Warden only ever mutates hc machines named ``wf-<fleet>-NN``,
terminals on them that carry the ``warden.module`` label, and spaces on them.
The operator's machines, terminals and spaces are never touched.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("warden.hc")

MACHINE_RE = re.compile(r"^wf-[a-z0-9]{8}-\d{2,}$")
TERMINAL_NAME_RE = re.compile(r"^([a-z][a-z0-9-]{0,23})-(\d{1,2})$")
MODULE_LABEL = "warden.module"

# (argv, timeout seconds) -> (returncode, stdout, stderr)
Runner = Callable[[list[str], float, bytes | None], tuple[int, str, str]]


class HcError(RuntimeError):
    def __init__(self, kind: str, message: str, rc: int = 1) -> None:
        super().__init__(f"hc {kind}: {message}")
        self.kind = kind
        self.message = message
        self.rc = rc


class OwnershipError(ValueError):
    """Refused: the target is not a Warden-owned machine/terminal/space."""


def _subprocess_runner(argv: list[str], timeout: float, data: bytes | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        argv,
        input=data,
        stdin=subprocess.DEVNULL if data is None else None,
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")


def guard_machine(name: str) -> str:
    if not MACHINE_RE.match(name or ""):
        raise OwnershipError(f"refusing to touch hc machine {name!r}: not a Warden fleet machine")
    return name


def guard_address(addr: str) -> str:
    machine, sep, term = (addr or "").partition("/")
    if not sep or not term:
        raise OwnershipError(f"refusing terminal address {addr!r}: expected <machine>/<terminal>")
    guard_machine(machine)
    return addr


def guard_terminal(name: str, labels: dict[str, str]) -> None:
    """A Warden module terminal is named ``<module>-<n>`` and labelled ``warden.module=<module>``."""
    m = TERMINAL_NAME_RE.match(name or "")
    if not m:
        raise OwnershipError(f"refusing terminal name {name!r}: not a Warden module slot")
    if labels.get(MODULE_LABEL) != m.group(1):
        raise OwnershipError(f"terminal {name!r} must carry label {MODULE_LABEL}={m.group(1)}")


@dataclass
class ExecResult:
    code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    signal: int | None = None


class HcDriver:
    def __init__(self, bin: str = "hc", runner: Runner | None = None) -> None:
        self.bin = bin
        self._runner = runner or _subprocess_runner

    # -- plumbing --

    def _call(self, args: list[str], timeout: float = 60, data: bytes | None = None) -> Any:
        rc, out, err = self._runner([self.bin, *args], timeout, data)
        data = _parse_json(out) if out.strip() else _parse_json(err)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            e = data["error"]
            raise HcError(str(e.get("kind", "error")), str(e.get("message", "")), rc)
        if rc != 0:
            raise HcError("error", (out + err).strip()[:300], rc)
        return data

    # -- core --

    def version(self) -> str:
        rc, out, err = self._runner([self.bin, "--version"], 15, None)
        if rc != 0:
            raise HcError("error", (out + err).strip()[:200], rc)
        return out.strip().split()[-1] if out.strip() else ""

    def server_status(self) -> dict:
        return self._call(["server", "status"], 15)

    # -- machines --

    def machines(self) -> list[dict]:
        return list(self._call(["machine", "ls", "--json"], 30).get("machines", []))

    def machine(self, name: str) -> dict | None:
        return next((m for m in self.machines() if m.get("name") == name), None)

    def add_machine(self, name: str, target: str, timeout: float = 300) -> dict:
        """Register (and install hc on) a fleet machine. Listed in Herdr.app."""
        guard_machine(name)
        return self._call(["machine", "add", target, "--name", name, "--install", "--json"], timeout)

    def remove_machine(self, name: str) -> bool:
        """Forget a fleet machine. Returns False if it was not registered."""
        guard_machine(name)
        try:
            self._call(["machine", "rm", name, "--json"], 60)
            return True
        except HcError as exc:
            if exc.kind == "machine_unknown":
                return False
            raise

    # -- terminals --

    def run(
        self, machine: str, name: str, labels: dict[str, str], argv: list[str], cwd: str | None = None
    ) -> dict:
        """Start ``argv`` in a new background terminal on a fleet machine; returns its address."""
        guard_machine(machine)
        guard_terminal(name, labels)
        args = ["run", "--on", machine, "--bg", "--no-parent", "--name", name]
        if cwd:
            args += ["--cwd", cwd]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        return self._call([*args, "--json", "--", *argv], 120)

    def get(self, addr: str) -> dict | None:
        guard_address(addr)
        try:
            return self._call(["get", addr, "--json"], 60)
        except HcError as exc:
            if exc.kind in ("not_found", "machine_unknown"):
                return None
            raise

    def list(self, labels: dict[str, str]) -> list[dict]:
        return self.listing(labels)[0]

    def listing(self, labels: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
        """Terminals carrying ``labels``, and each machine's connection state (``connected``, ...).

        A machine hc cannot reach right now lists no terminals, so its state says
        whether an empty listing means "none" or "unknown".
        """
        if not labels or not all(k.startswith("warden.") for k in labels):
            raise OwnershipError("terminal listings must filter on warden.* labels")
        args = ["ls", "--all"]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        out = self._call([*args, "--json"], 60)
        machines = {m["name"]: m.get("state", "") for m in out.get("machines", []) if m.get("name")}
        return list(out.get("terminals", [])), machines

    def interrupt(self, addr: str) -> dict:
        guard_address(addr)
        return self._call(["interrupt", addr, "--json"], 60)

    def kill(self, addr: str) -> bool:
        """End a terminal's program (hangup, then escalate). False if it was already gone."""
        guard_address(addr)
        try:
            self._call(["kill", addr, "--json"], 60)
            return True
        except HcError as exc:
            if exc.kind in ("not_found", "machine_unknown"):
                return False
            raise

    def remove(self, addr: str) -> bool:
        guard_address(addr)
        try:
            self._call(["rm", addr, "--json"], 60)
            return True
        except HcError as exc:
            if exc.kind in ("not_found", "machine_unknown"):
                return False
            raise

    def exec(self, machine: str, argv: list[str], timeout: int = 60) -> ExecResult:
        guard_machine(machine)
        timeout = max(1, min(int(timeout), 600))
        data = self._call(
            ["exec", "--on", machine, "--timeout", str(timeout), "--json", "--", *argv],
            timeout + 30,
        )
        return ExecResult(
            code=data.get("code"),
            stdout=_b64(data.get("stdout")),
            stderr=_b64(data.get("stderr")),
            timed_out=bool(data.get("timed_out")),
            signal=data.get("signal"),
        )

    def write_file(self, machine: str, path: str, data: bytes) -> None:
        """Write ``data`` to ``path`` on a fleet machine (parent folders are created)."""
        guard_machine(machine)
        self._call(["file", "write", "--on", machine, "--json", path], 300, data)

    def shell(self, machine: str, script: str, timeout: int = 60) -> ExecResult:
        """Run a bash snippet on a fleet machine."""
        return self.exec(machine, ["bash", "-c", script], timeout)


def _parse_json(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        # Some commands print a human line before/after; take the first JSON line.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return {"_raw": text}


def _b64(value: Any) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return str(value)


def prune_known_hosts(hosts: list[str], known_hosts_file: Path) -> None:
    """Remove fleet hosts from Warden's dedicated known_hosts file only."""
    path = Path(known_hosts_file).expanduser()
    if not path.exists():
        return
    for host in hosts:
        guard_machine(host.split(".", 1)[0])
        subprocess.run(
            ["ssh-keygen", "-R", host, "-f", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )


# --- Herdr.app window -----------------------------------------------------------

SPACE_PREFIX = "warden "
SPACE_LABEL = "herdr.space"


class HerdrApp:
    """Show fleet work in Herdr.app via its documented ``herdr-app`` CLI.

    Herdr.app only shows hc terminals that opt in with a ``herdr.space=<id>``
    label; Herdr then gives each one a tab in that space. Warden keeps one
    ``warden <machine>`` space per fleet machine, created *on* that machine
    (``space.new --machine``), so its jobs appear under the machine.

    Everything here is best effort: with no Herdr window open, calls fail,
    jobs still run (hidden), and the caller logs and carries on. Only spaces
    on ``wf-*`` machines are ever closed.
    """

    def __init__(self, bin: str = "herdr-app", runner: Runner | None = None) -> None:
        self.bin = bin
        self._runner = runner or _subprocess_runner

    def _call(self, args: list[str], timeout: float = 30) -> Any:
        rc, out, err = self._runner([self.bin, *args], timeout, None)
        data = _parse_json(out) if out.strip() else _parse_json(err)
        if isinstance(data, dict) and data.get("error"):
            raise HcError("herdr_app", str(data["error"]), rc)
        if rc != 0:
            raise HcError("herdr_app", (out + err).strip()[:300], rc)
        return data

    def spaces(self) -> list[dict]:
        return list(self._call(["ls"]).get("spaces", []))

    def spaces_on(self, machine: str) -> list[dict]:
        guard_machine(machine)
        return [s for s in self.spaces() if s.get("machine") == machine]

    def ensure_space(self, machine: str, folder: str = "/root") -> str:
        """Machine-local id of the ``warden <machine>`` space, creating it if needed.

        ``herdr-app ls`` reports remote space ids as ``<machine>/<id>``; a
        terminal running *on* that machine must be labelled with ``<id>``
        (with the prefix, Herdr opens a separate space instead).
        """
        name = f"{SPACE_PREFIX}{guard_machine(machine)}"
        found = next((s for s in self.spaces_on(machine) if s.get("name") == name), None)
        if found is None:
            self._call(
                [
                    "do",
                    "space.new",
                    "--name",
                    name,
                    "--machine",
                    machine,
                    "--folder",
                    folder,
                    "--tab",
                    "shell",
                ]
            )
            found = next((s for s in self.spaces_on(machine) if s.get("name") == name), None)
        if found is None:
            raise HcError("herdr_app", f"space {name!r} not visible after space.new")
        return str(found["id"]).removeprefix(f"{machine}/")

    def name_tab(self, machine: str, terminal_address: str, name: str, wait_s: float = 5.0) -> bool:
        """Rename the tab Herdr made for a terminal (it appears shortly after the terminal starts)."""
        guard_address(terminal_address)
        deadline = time.monotonic() + wait_s
        while True:
            for space in self.spaces_on(machine):
                for tab in space.get("tabs", []):
                    if any(t.get("terminal") == terminal_address for t in tab.get("terminals", [])):
                        self._call(["do", "tab.rename", "--id", str(tab["id"]), "--name", name])
                        return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def close_spaces_on(self, machine: str) -> int:
        """Close every space on a Warden fleet machine (ends its terminals)."""
        closed = 0
        for s in self.spaces_on(machine):
            self._call(["do", "space.close", "--id", str(s["id"]), "--confirmed", "true"])
            closed += 1
        return closed
