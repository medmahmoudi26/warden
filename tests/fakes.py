"""In-memory fakes shared by unit tests: hc, Herdr.app, Tailscale API, Pulumi provider, Temporal client."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from warden.fleet.models import MachineEntry
from warden.hc import ExecResult, HcError, OwnershipError, guard_address, guard_machine, guard_terminal

OPERATOR_MACHINE = "203.0.113.10"


class FakeHc:
    """Machines, labelled terminals, files and slot exit codes, mimicking the hc CLI's behaviour.

    ``bash -c`` snippets are recognised by what they do: the Temporal probe, a
    module unpack, listing or clearing slot exit files.
    """

    def __init__(self, *, add_failures: int = 0, boot: str = "status: done\n", temporal_ok: bool = True):
        self.add_failures = add_failures
        self.boot = boot
        self.temporal_ok = temporal_ok
        self.disconnected: set[str] = set()  # machines hc cannot reach right now
        self.registered: dict[str, dict] = {
            OPERATOR_MACHINE: {"name": OPERATOR_MACHINE, "state": "connected"}
        }
        self.removed_machines: list[str] = []
        self.terminals: dict[str, dict] = {}  # address -> {name, labels, state, argv, cwd}
        self.files: dict[tuple[str, str], bytes] = {}
        self.rc: dict[str, dict[str, int]] = {}  # machine -> slot name -> exit code
        self.shipped: list[tuple[str, str]] = []  # (machine, module)
        self.fail_write: set[str] = set()
        self.interrupt_exits = True  # a worker that exits on ctrl+c writes its rc file
        self.interrupted: list[str] = []
        self.killed: list[str] = []
        self.calls: list[tuple] = []
        self._n = 0

    # -- core / machines --

    def version(self) -> str:
        return "0.1.18"

    def server_status(self) -> dict:
        return {"version": "0.1.18"}

    def add_machine(self, name: str, target: str, timeout: float = 300) -> dict:
        guard_machine(name)
        if self.add_failures:
            self.add_failures -= 1
            raise HcError("error", "ssh: connect refused")
        self.registered[name] = {"name": name, "state": "connected", "version": "0.1.17", "target": target}
        return self.registered[name]

    def machine(self, name: str) -> dict | None:
        return self.registered.get(name)

    def machines(self) -> list[dict]:
        return list(self.registered.values())

    def remove_machine(self, name: str) -> bool:
        guard_machine(name)
        if name in self.registered:
            del self.registered[name]
            self.removed_machines.append(name)
            return True
        return False

    # -- terminals --

    def run(self, machine: str, name: str, labels: dict[str, str], argv: list[str], cwd: str | None = None):
        guard_machine(machine)
        guard_terminal(name, labels)
        if any(
            t["name"] == name and t["state"] == "live" and a.startswith(f"{machine}/")
            for a, t in self.terminals.items()
        ):
            raise HcError("conflict", f"a live terminal is already named {name}")
        self._n += 1
        addr = f"{machine}/t{self._n}"
        self.terminals[addr] = {
            "name": name,
            "labels": dict(labels),
            "state": "live",
            "argv": argv,
            "cwd": cwd,
        }
        self.rc.get(machine, {}).pop(name, None)  # the slot wrapper clears its exit file
        self.calls.append(("run", machine, name))
        return {"address": addr, "name": name, "state": "live"}

    def list(self, labels: dict[str, str]) -> list[dict]:
        return self.listing(labels)[0]

    def listing(self, labels: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
        """Like hc: terminals of machines that are not connected are left out."""
        if not labels or not all(k.startswith("warden.") for k in labels):
            raise OwnershipError("terminal listings must filter on warden.* labels")
        machines = set(self.registered) | {a.split("/", 1)[0] for a in self.terminals}
        states = {m: "disconnected" if m in self.disconnected else "connected" for m in machines}
        terms = [
            {"address": a, "name": t["name"], "state": t["state"]}
            for a, t in self.terminals.items()
            if all(t["labels"].get(k) == v for k, v in labels.items())
            and states.get(a.split("/", 1)[0], "connected") == "connected"
        ]
        return terms, states

    def get(self, addr: str) -> dict | None:
        guard_address(addr)
        t = self.terminals.get(addr)
        return {"address": addr, **t} if t else None

    def interrupt(self, addr: str) -> dict:
        guard_address(addr)
        self.interrupted.append(addr)
        t = self.terminals.get(addr)
        if t and self.interrupt_exits:
            self.exit_slot(addr.split("/")[0], t["name"], 0)
        return {"key": "ctrl+c", "outcome": "exited"}

    def kill(self, addr: str) -> bool:
        guard_address(addr)
        t = self.terminals.get(addr)
        if t is None:
            return False
        t["state"] = "finished"
        self.killed.append(addr)
        return True

    def remove(self, addr: str) -> bool:
        guard_address(addr)
        return self.terminals.pop(addr, None) is not None

    def exit_slot(self, machine: str, name: str, rc: int) -> None:
        """Simulate an entrypoint exiting: its exit file appears, the terminal stays (held shell)."""
        self.rc.setdefault(machine, {})[name] = rc

    # -- exec / files --

    def exec(self, machine: str, argv: list[str], timeout: int = 60) -> ExecResult:
        guard_machine(machine)
        self.calls.append(("exec", machine, tuple(argv)))
        if argv[:1] == ["cloud-init"]:
            return ExecResult(code=0, stdout=self.boot, stderr="")
        if argv[:2] == ["bash", "-c"]:
            return self._bash(machine, argv[2])
        return ExecResult(code=0, stdout="", stderr="")

    def shell(self, machine: str, script: str, timeout: int = 60) -> ExecResult:
        return self.exec(machine, ["bash", "-c", script], timeout)

    def write_file(self, machine: str, path: str, data: bytes) -> None:
        guard_machine(machine)
        if machine in self.fail_write:
            raise HcError("error", f"write {path}: connection lost")
        self.files[(machine, path)] = data

    def _bash(self, machine: str, script: str) -> ExecResult:
        if "</dev/tcp/" in script:
            return ExecResult(code=0 if self.temporal_ok else 1, stdout="", stderr="")
        if "tar -xzf" in script:
            module = re.search(r"/opt/warden/modules/([a-z0-9-]+)\.new", script).group(1)  # type: ignore[union-attr]
            self.shipped.append((machine, module))
            return ExecResult(code=0, stdout="", stderr="")
        if script.startswith("cd /var/lib/warden/slots"):
            lines = "".join(f"{n} {rc}\n" for n, rc in sorted(self.rc.get(machine, {}).items()))
            return ExecResult(code=0, stdout=lines, stderr="")
        m = re.match(r"rm -f /var/lib/warden/slots/([a-z0-9-]+)-\*\.rc", script)
        if m:
            slots = self.rc.get(machine, {})
            for name in [n for n in slots if n.startswith(f"{m.group(1)}-")]:
                del slots[name]
        return ExecResult(code=0, stdout="", stderr="")


class FakeHerdr:
    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.space_ids: dict[str, str] = {}
        self.named: list[tuple[str, str, str]] = []
        self.closed: list[str] = []

    def ensure_space(self, machine: str) -> str:
        guard_machine(machine)
        if self.broken:
            raise HcError("herdr_app", "no Herdr window is running")
        return self.space_ids.setdefault(machine, f"sp-{len(self.space_ids) + 1}")

    def spaces(self) -> list[dict]:
        if self.broken:
            raise HcError("herdr_app", "no Herdr window is running")
        return [
            {"id": f"{m}/{sid}", "name": f"warden {m}", "machine": m} for m, sid in self.space_ids.items()
        ]

    def name_tab(self, machine: str, address: str, name: str, wait_s: float = 5.0) -> bool:
        self.named.append((machine, address, name))
        return True

    def close_spaces_on(self, machine: str) -> int:
        guard_machine(machine)
        self.closed.append(machine)
        return 1 if self.space_ids.pop(machine, None) else 0


@dataclass
class Dev:
    id: str
    hostname: str
    online: bool = True
    tags: list[str] = field(default_factory=lambda: ["tag:warden-worker"])


class FakeTs:
    def __init__(self, devices: list[Dev] | None = None) -> None:
        self.devs = list(devices or [])
        self.deleted: list[str] = []

    def devices(self, *, tag: str | None = None, hostname_prefix: str = "") -> list[Dev]:
        return [d for d in self.devs if d.hostname.startswith(hostname_prefix)]

    def delete_device(self, device_id: str) -> bool:
        if any(d.id == device_id for d in self.devs):
            self.devs = [d for d in self.devs if d.id != device_id]
            self.deleted.append(device_id)
            return True
        return False


class FakeProvider:
    def __init__(self) -> None:
        self.ups: list[str] = []
        self.destroys: list[str] = []
        self.stacks: set[str] = set()

    def up(self, spec) -> list[MachineEntry]:
        self.ups.append(spec.fleet_id)
        self.stacks.add(spec.fleet_id)
        return [
            MachineEntry(
                name=f"wf-{spec.fleet_id}-{i + 1:02d}", tailnet_host=f"wf-{spec.fleet_id}-{i + 1:02d}.t"
            )
            for i in range(spec.instances)
        ]

    def destroy(self, fleet_id: str) -> None:
        self.destroys.append(fleet_id)
        self.stacks.discard(fleet_id)

    def has_stack(self, fleet_id: str) -> bool:
        return fleet_id in self.stacks


class FakeHandle:
    def __init__(self, client: FakeClient, wid: str) -> None:
        self.client, self.id = client, wid

    async def terminate(self, reason: str | None = None) -> None:
        self.client.terminated.append((self.id, reason))
        self.client.running.remove(self.id)

    async def cancel(self) -> None:
        self.client.cancelled.append(self.id)


class FakeClient:
    """Just enough of temporalio.client.Client for listing, terminating and cancelling workflows."""

    def __init__(self, running: list[str] | None = None) -> None:
        self.running = list(running or [])
        self.terminated: list[tuple[str, str | None]] = []
        self.cancelled: list[str] = []
        self.queries: list[str] = []

    async def list_workflows(self, query: str):
        self.queries.append(query)
        for wid in list(self.running):
            yield type("WF", (), {"id": wid})()

    def get_workflow_handle(self, wid: str, **_: Any) -> FakeHandle:
        return FakeHandle(self, wid)
