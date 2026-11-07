"""Modules: folders with an ``entrypoint.sh``, run on every fleet machine in visible hc terminals.

Warden does not care what an entrypoint runs. Activation copies the folder to
``/opt/warden/modules/<module>`` on each machine and starts one terminal per
worker slot, named ``<module>-<n>`` and shown as the tab ``<module> #<n>`` in the
machine's ``warden <machine>`` space in Herdr.app.

Nothing about modules is recorded on the control plane: which slots exist comes
from ``hc`` terminal labels, and whether a slot's entrypoint has exited comes
from its exit-code file on the machine. This module only talks to hc.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tarfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from warden.config import Settings
from warden.fleet.provider import MODULES_ROOT, SLOTS_DIR
from warden.hc import TERMINAL_NAME_RE, HcError

logger = logging.getLogger("warden.modules")

MODULE_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
MAX_WORKERS = 8
EXCLUDED = {".venv", "venv", "__pycache__", ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
RC_LIST = (
    f"cd {SLOTS_DIR} 2>/dev/null || exit 0; "
    'for f in *.rc; do [ -e "$f" ] || continue; printf "%s %s\\n" "${f%.rc}" "$(cat "$f")"; done'
)

# Runs in each slot's terminal. The trailing login shell keeps the terminal (and
# its Herdr tab) open after the entrypoint exits, so its output stays visible.
SLOT_WRAPPER = r"""mkdir -p "$(dirname "$WARDEN_SLOT_RC")"; rm -f "$WARDEN_SLOT_RC"
if cd "/opt/warden/modules/$WARDEN_MODULE"; then ./entrypoint.sh; rc=$?; else rc=127; fi
echo "$rc" > "$WARDEN_SLOT_RC"
printf '\n[warden] %s #%s exited rc=%s\n' "$WARDEN_MODULE" "$WARDEN_WORKER" "$rc"
exec bash -l"""


class UnknownModuleError(LookupError):
    pass


class ModuleError(RuntimeError):
    pass


@dataclass
class Module:
    name: str
    path: Path
    description: str = ""


@dataclass
class SlotState:
    machine: str
    module: str
    slot: int
    state: str  # running | exited
    rc: int | None = None
    address: str = ""


@dataclass
class MachineOutcome:
    machine: str
    started: list[int] = field(default_factory=list)
    already_running: list[int] = field(default_factory=list)
    shipped: bool = False
    error: str | None = None


def slot_name(module: str, n: int) -> str:
    return f"{module}-{n}"


def worker_id(machine: str, module: str, n: int) -> str:
    return f"{machine}/{slot_name(module, n)}"


def task_queue(fleet_id: str, module: str) -> str:
    return f"{fleet_id}.{module}"


def describe(entrypoint: Path) -> str:
    """First comment line of an entrypoint, after any shebang."""
    try:
        lines = entrypoint.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#!") or not stripped:
            continue
        return stripped.lstrip("#").strip() if stripped.startswith("#") else ""
    return ""


def discover(root: Path) -> list[Module]:
    """Modules directly under ``root``: valid name and an executable ``entrypoint.sh``."""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for path in sorted(root.iterdir()):
        entry = path / "entrypoint.sh"
        if path.is_dir() and MODULE_RE.match(path.name) and entry.is_file() and os.access(entry, os.X_OK):
            out.append(Module(name=path.name, path=path, description=describe(entry)))
    return out


def pack(path: Path) -> bytes:
    """Gzip tarball of a module folder, without virtualenvs, caches or VCS folders."""

    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(p in EXCLUDED for p in parts) or info.name.endswith((".pyc", ".pyo")):
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for child in sorted(Path(path).iterdir()):
            tar.add(child, arcname=child.name, filter=keep)
    return buf.getvalue()


T = TypeVar("T")


def _each(machines: list[str], fn: Callable[[str], T]) -> list[T]:
    if not machines:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(machines))) as pool:
        return list(pool.map(fn, machines))


def _machine_of(address: str) -> str:
    return address.split("/", 1)[0]


class Modules:
    def __init__(
        self,
        settings: Settings,
        hc: Any,
        herdr: Any,
        *,
        stop_grace_s: float = 30.0,
        poll_s: float = 1.0,
    ) -> None:
        self.settings = settings
        self.hc = hc
        self.herdr = herdr
        self.stop_grace_s = stop_grace_s
        self.poll_s = poll_s

    # --- listing ---------------------------------------------------------------

    def list(self) -> list[Module]:
        return discover(self.settings.modules_dir)

    def get(self, name: str) -> Module:
        found = {m.name: m for m in self.list()}
        if name not in found:
            available = ", ".join(sorted(found)) or "none"
            raise UnknownModuleError(f"unknown module {name!r}; available: {available}")
        return found[name]

    # --- state -----------------------------------------------------------------

    def _rc_files(self, machine: str) -> dict[str, int | None]:
        """Slot name -> exit code, from the machine's slot exit files."""
        try:
            r = self.hc.shell(machine, RC_LIST, 30)
        except Exception as exc:  # noqa: BLE001 — an unreachable machine reports no exits
            logger.warning("reading slot exit codes on %s: %s", machine, exc)
            return {}
        out: dict[str, int | None] = {}
        for line in r.stdout.splitlines():
            name, _, rc = line.strip().partition(" ")
            if name:
                out[name] = int(rc) if rc.strip().lstrip("-").isdigit() else None
        return out

    def _terminals(self, fleet_id: str, module: str | None = None) -> list[dict]:
        labels = {"warden.fleet": fleet_id}
        if module:
            labels["warden.module"] = module
        return [t for t in self.hc.list(labels) if TERMINAL_NAME_RE.match(t.get("name", ""))]

    def state(self, fleet_id: str, machines: list[str]) -> list[SlotState]:
        """Every module slot on the fleet, running or exited."""
        return self.snapshot(fleet_id, machines)[0]

    def snapshot(self, fleet_id: str, machines: list[str]) -> tuple[list[SlotState], dict[str, str]]:
        """The fleet's module slots, and each machine's hc connection state.

        hc lists no terminals for a machine it cannot reach, so a machine whose state
        is not ``connected`` has slots that are unknown, not absent.
        """
        listed, hc_states = self.hc.listing({"warden.fleet": fleet_id})
        terms = [t for t in listed if TERMINAL_NAME_RE.match(t.get("name", ""))]
        reachable = [m for m in machines if hc_states.get(m, "connected") == "connected"]
        rcs = dict(zip(reachable, _each(reachable, self._rc_files), strict=True))
        out = []
        for t in terms:
            machine = _machine_of(t["address"])
            m = TERMINAL_NAME_RE.match(t["name"])
            assert m is not None
            module, n = m.group(1), int(m.group(2))
            exits = rcs.get(machine, {})
            if t.get("state") == "live" and t["name"] not in exits:
                state, rc = "running", None
            else:
                state, rc = "exited", exits.get(t["name"])
            out.append(SlotState(machine, module, n, state, rc, t["address"]))
        slots = sorted(out, key=lambda s: (s.machine, s.module, s.slot))
        return slots, {m: hc_states.get(m, "unknown") for m in machines}

    # --- activate --------------------------------------------------------------

    def activate(
        self, fleet_id: str, machines: list[str], module: str, workers: int = 1
    ) -> list[MachineOutcome]:
        """Start missing worker slots of ``module`` on every machine (idempotent)."""
        if not 1 <= workers <= MAX_WORKERS:
            raise ValueError(f"workers must be between 1 and {MAX_WORKERS}, got {workers}")
        mod = self.get(module)
        blob = pack(mod.path)
        temporal = self.settings.fleet_temporal_address
        by_machine: dict[str, dict[str, dict]] = {}
        for t in self._terminals(fleet_id, module):
            by_machine.setdefault(_machine_of(t["address"]), {})[t["name"]] = t
        return _each(
            machines,
            lambda machine: self._activate_on(
                fleet_id, machine, module, workers, blob, temporal, by_machine.get(machine, {})
            ),
        )

    def _activate_on(
        self,
        fleet_id: str,
        machine: str,
        module: str,
        workers: int,
        blob: bytes,
        temporal: str,
        existing: dict[str, dict],
    ) -> MachineOutcome:
        out = MachineOutcome(machine)
        try:
            exits = self._rc_files(machine)
            running = {
                int(TERMINAL_NAME_RE.match(name).group(2))  # type: ignore[union-attr]
                for name, t in existing.items()
                if t.get("state") == "live" and name not in exits
            }
            out.already_running = sorted(running)
            if not running:  # never swap code under a running worker
                self._ship(machine, module, blob)
                out.shipped = True
            space = self._space(machine)
            for n in range(1, workers + 1):
                if n in running:
                    continue
                name = slot_name(module, n)
                old = existing.get(name)
                if old is not None:  # an exited slot: replace its terminal
                    self.hc.kill(old["address"])
                    self.hc.remove(old["address"])
                started = self.hc.run(
                    machine,
                    name,
                    self._labels(fleet_id, module, n, space),
                    self._argv(fleet_id, machine, module, n, temporal),
                    cwd=f"{MODULES_ROOT}/{module}",
                )
                out.started.append(n)
                if space:
                    try:
                        self.herdr.name_tab(machine, started["address"], f"{module} #{n}")
                    except Exception as exc:  # noqa: BLE001 — cosmetic
                        logger.info("herdr: naming tab %s on %s: %s", name, machine, exc)
        except Exception as exc:  # noqa: BLE001 — reported per machine
            out.error = str(exc)
        return out

    def _ship(self, machine: str, module: str, blob: bytes) -> None:
        tmp = f"/tmp/warden-module-{module}.tgz"
        self.hc.write_file(machine, tmp, blob)
        dest = f"{MODULES_ROOT}/{module}"
        script = (
            f"set -e; rm -rf {dest}.new; mkdir -p {dest}.new; tar -xzf {tmp} -C {dest}.new; "
            f"chmod +x {dest}.new/entrypoint.sh; rm -rf {dest}; mv {dest}.new {dest}; rm -f {tmp}"
        )
        r = self.hc.shell(machine, script, 120)
        if r.code != 0:
            raise ModuleError(f"unpacking {module} failed: {(r.stderr or r.stdout).strip()[:300]}")

    def _space(self, machine: str) -> str | None:
        try:
            return self.herdr.ensure_space(machine)
        except Exception as exc:  # noqa: BLE001 — the Herdr window is best effort
            logger.warning("herdr: no space on %s (%s); slots run without a tab", machine, exc)
            return None

    @staticmethod
    def _labels(fleet_id: str, module: str, n: int, space: str | None) -> dict[str, str]:
        labels = {"warden.fleet": fleet_id, "warden.module": module, "warden.slot": str(n)}
        if space:
            labels["herdr.space"] = space
        return labels

    @staticmethod
    def _argv(fleet_id: str, machine: str, module: str, n: int, temporal: str) -> list[str]:
        env = {
            "WARDEN_FLEET": fleet_id,
            "WARDEN_MACHINE": machine,
            "WARDEN_MODULE": module,
            "WARDEN_WORKER": str(n),
            "WARDEN_WORKER_ID": worker_id(machine, module, n),
            "WARDEN_TASK_QUEUE": task_queue(fleet_id, module),
            "TEMPORAL_ADDRESS": temporal,
            "WARDEN_SLOT_RC": f"{SLOTS_DIR}/{slot_name(module, n)}.rc",
        }
        return ["env", *(f"{k}={v}" for k, v in env.items()), "bash", "-c", SLOT_WRAPPER]

    # --- deactivate ------------------------------------------------------------

    def deactivate(self, fleet_id: str, machines: list[str], module: str) -> dict[str, int]:
        """Interrupt every slot of ``module``, wait for them to exit, then end and remove the terminals."""
        terms = self._terminals(fleet_id, module)
        pending = {t["address"]: t for t in terms if t.get("state") == "live"}
        for addr in pending:
            try:
                self.hc.interrupt(addr)
            except HcError as exc:
                logger.info("interrupt %s: %s", addr, exc)
        deadline = time.monotonic() + self.stop_grace_s
        while pending:
            on = sorted({_machine_of(a) for a in pending})
            exits = dict(zip(on, _each(on, self._rc_files), strict=True))
            for addr, t in list(pending.items()):
                if t["name"] in exits.get(_machine_of(addr), {}):
                    pending.pop(addr)
                    continue
                now = self.hc.get(addr)
                if now is None or now.get("state") != "live":
                    pending.pop(addr)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(self.poll_s)

        stopped: dict[str, int] = {m: 0 for m in machines}
        for t in terms:
            self.hc.kill(t["address"])
            self.hc.remove(t["address"])
            machine = _machine_of(t["address"])
            stopped[machine] = stopped.get(machine, 0) + 1
        for machine in {_machine_of(t["address"]) for t in terms}:
            try:
                self.hc.shell(machine, f"rm -f {SLOTS_DIR}/{module}-*.rc", 30)
            except Exception as exc:  # noqa: BLE001 — leftovers are overwritten by the next launch
                logger.info("clearing exit files on %s: %s", machine, exc)
        return stopped
