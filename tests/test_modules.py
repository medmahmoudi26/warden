"""modules.py: discovery, packing, activation, slot state and deactivation over a fake hc."""

from __future__ import annotations

import io
import stat
import tarfile
from pathlib import Path

import pytest

from tests.fakes import FakeHc, FakeHerdr
from warden.config import load_settings
from warden.modules import SLOT_WRAPPER, Modules, UnknownModuleError, discover, pack

M1, M2 = "wf-abcd1234-01", "wf-abcd1234-02"
FLEET = "abcd1234"


def write_module(
    root: Path,
    name: str,
    *,
    entry: str | None = "#!/usr/bin/env bash\n# Does a thing.\necho hi\n",
    executable: bool = True,
) -> Path:
    path = root / name
    path.mkdir(parents=True)
    if entry is not None:
        e = path / "entrypoint.sh"
        e.write_text(entry)
        if executable:
            e.chmod(e.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "modules"
    sub = write_module(
        r,
        "subdomains",
        entry="#!/usr/bin/env bash\n# Passive subdomain enumeration.\nexec uv run worker.py\n",
    )
    (sub / "worker.py").write_text("print('w')\n")
    for junk in (".venv/lib/x.py", "__pycache__/w.cpython-313.pyc", ".git/HEAD", "old.pyc"):
        (sub / junk).parent.mkdir(parents=True, exist_ok=True)
        (sub / junk).write_text("junk")
    write_module(r, "_lib")
    write_module(r, "noentry", entry=None)
    write_module(r, "notexec", executable=False)
    write_module(r, "Bad_Name")
    write_module(r, "plain", entry="#!/bin/sh\necho no description\n")
    return r


def make(root: Path, hc: FakeHc | None = None, herdr: FakeHerdr | None = None, **kw) -> Modules:
    settings = load_settings({"WARDEN_MODULES_DIR": str(root), "WARDEN_CONTROL_HOST": "mac.tail.ts.net"})
    return Modules(settings, hc or FakeHc(), herdr or FakeHerdr(), **kw)


def slot_runs(hc: FakeHc) -> list[tuple[str, str]]:
    return [(c[1], c[2]) for c in hc.calls if c[0] == "run"]


# --- discovery ---------------------------------------------------------------------


def test_discover_lists_valid_modules_with_descriptions(root: Path) -> None:
    found = {m.name: m.description for m in discover(root)}
    assert found == {"plain": "", "subdomains": "Passive subdomain enumeration."}


def test_unknown_module_lists_available(root: Path) -> None:
    with pytest.raises(UnknownModuleError, match="available: plain, subdomains"):
        make(root).get("noentry")
    assert discover(root / "missing") == []


def test_pack_excludes_envs_caches_and_vcs(root: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(pack(root / "subdomains")), mode="r:gz") as tar:
        names = sorted(tar.getnames())
        entry = tar.getmember("entrypoint.sh")
        assert entry.mode & stat.S_IXUSR
        assert tar.extractfile("worker.py").read() == b"print('w')\n"  # type: ignore[union-attr]
    assert names == ["entrypoint.sh", "worker.py"]


def test_pack_ships_the_git_ignored_provider_config(root: Path) -> None:
    (root / "subdomains" / "config").mkdir()
    (root / "subdomains" / "config" / "provider-config.yaml").write_text("shodan: [k]\n")
    with tarfile.open(fileobj=io.BytesIO(pack(root / "subdomains")), mode="r:gz") as tar:
        assert "config/provider-config.yaml" in tar.getnames()


# --- activation ----------------------------------------------------------------------


def test_activate_ships_and_starts_one_slot_per_machine(root: Path) -> None:
    hc, herdr = FakeHc(), FakeHerdr()
    out = make(root, hc, herdr).activate(FLEET, [M1, M2], "subdomains")
    assert [(o.machine, o.started, o.shipped, o.error) for o in out] == [
        (M1, [1], True, None),
        (M2, [1], True, None),
    ]
    assert sorted(hc.shipped) == [(M1, "subdomains"), (M2, "subdomains")]
    tgz = hc.files[(M1, "/tmp/warden-module-subdomains.tgz")]
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tar:
        assert tar.getnames()  # a real tarball
    addr, term = next((a, t) for a, t in hc.terminals.items() if a.startswith(M2))
    assert term["name"] == "subdomains-1" and term["cwd"] == "/opt/warden/modules/subdomains"
    assert term["labels"] == {
        "warden.fleet": FLEET,
        "warden.module": "subdomains",
        "warden.slot": "1",
        "herdr.space": herdr.space_ids[M2],
    }
    argv = term["argv"]
    assert argv[0] == "env" and argv[-3:] == ["bash", "-c", SLOT_WRAPPER]
    env = dict(a.split("=", 1) for a in argv[1:-3])
    assert env == {
        "WARDEN_FLEET": FLEET,
        "WARDEN_MACHINE": M2,
        "WARDEN_MODULE": "subdomains",
        "WARDEN_WORKER": "1",
        "WARDEN_WORKER_ID": f"{M2}/subdomains-1",
        "WARDEN_TASK_QUEUE": f"{FLEET}.subdomains",
        "TEMPORAL_ADDRESS": "mac.tail.ts.net:7233",
        "WARDEN_SLOT_RC": "/var/lib/warden/slots/subdomains-1.rc",
    }
    assert (M2, addr, "subdomains #1") in herdr.named


def test_activate_twice_starts_nothing(root: Path) -> None:
    hc = FakeHc()
    mods = make(root, hc)
    mods.activate(FLEET, [M1], "subdomains")
    again = mods.activate(FLEET, [M1], "subdomains")
    assert [(o.started, o.already_running, o.shipped) for o in again] == [([], [1], False)]
    assert len(slot_runs(hc)) == 1 and len(hc.shipped) == 1


def test_scale_up_starts_only_missing_slots(root: Path) -> None:
    hc = FakeHc()
    mods = make(root, hc)
    mods.activate(FLEET, [M1], "subdomains")
    [o] = mods.activate(FLEET, [M1], "subdomains", workers=3)
    assert (o.started, o.already_running, o.shipped) == ([2, 3], [1], False)  # code not swapped under slot 1
    assert hc.killed == [] and [n for _, n in slot_runs(hc)] == [
        "subdomains-1",
        "subdomains-2",
        "subdomains-3",
    ]


def test_exited_slot_is_replaced_and_code_reshipped(root: Path) -> None:
    hc = FakeHc()
    mods = make(root, hc)
    mods.activate(FLEET, [M1], "subdomains")
    old = next(iter(hc.terminals))
    hc.exit_slot(M1, "subdomains-1", 1)
    [o] = mods.activate(FLEET, [M1], "subdomains")
    assert (o.started, o.already_running, o.shipped) == ([1], [], True)
    assert hc.killed == [old] and old not in hc.terminals


def test_failing_machine_is_reported_others_succeed(root: Path) -> None:
    hc = FakeHc()
    hc.fail_write.add(M2)
    out = make(root, hc).activate(FLEET, [M1, M2], "subdomains")
    by = {o.machine: o for o in out}
    assert by[M1].started == [1] and by[M1].error is None
    assert by[M2].started == [] and "connection lost" in (by[M2].error or "")


def test_activate_without_herdr_window_runs_hidden(root: Path) -> None:
    hc = FakeHc()
    [o] = make(root, hc, FakeHerdr(broken=True)).activate(FLEET, [M1], "subdomains")
    assert o.started == [1] and o.error is None
    [term] = hc.terminals.values()
    assert "herdr.space" not in term["labels"]


def test_activate_rejects_bad_worker_counts_and_unknown_modules(root: Path) -> None:
    mods = make(root)
    with pytest.raises(ValueError, match="workers"):
        mods.activate(FLEET, [M1], "subdomains", workers=0)
    with pytest.raises(UnknownModuleError):
        mods.activate(FLEET, [M1], "nope")


# --- state ---------------------------------------------------------------------------


def test_state_reports_running_and_exited_slots(root: Path) -> None:
    hc = FakeHc()
    mods = make(root, hc)
    mods.activate(FLEET, [M1, M2], "subdomains", workers=2)
    hc.exit_slot(M2, "subdomains-2", 3)
    states = [(s.machine, s.module, s.slot, s.state, s.rc) for s in mods.state(FLEET, [M1, M2])]
    assert states == [
        (M1, "subdomains", 1, "running", None),
        (M1, "subdomains", 2, "running", None),
        (M2, "subdomains", 1, "running", None),
        (M2, "subdomains", 2, "exited", 3),
    ]
    assert make(root, hc).state("zzzz9999", [M1]) == []  # another fleet: nothing


# --- deactivation ----------------------------------------------------------------------


def raw_terminal(hc: FakeHc, machine: str, name: str, labels: dict) -> str:
    addr = f"{machine}/t9{len(hc.terminals)}"
    hc.terminals[addr] = {"name": name, "labels": labels, "state": "live", "argv": ["nmap"], "cwd": None}
    return addr


def test_deactivate_stops_only_that_modules_slots(root: Path) -> None:
    hc = FakeHc()
    mods = make(root, hc, stop_grace_s=1, poll_s=0.01)
    mods.activate(FLEET, [M1, M2], "subdomains", workers=2)
    mods.activate(FLEET, [M1], "plain")
    agent = raw_terminal(hc, M1, "nmap-scan", {"herdr.space": "sp-1"})
    assert mods.deactivate(FLEET, [M1, M2], "subdomains") == {M1: 2, M2: 2}
    assert len(hc.interrupted) == 4
    left = sorted(t["name"] for t in hc.terminals.values())
    assert left == ["nmap-scan", "plain-1"] and hc.terminals[agent]["state"] == "live"
    assert all(not n.startswith("subdomains-") for slots in hc.rc.values() for n in slots)
    assert [s.module for s in mods.state(FLEET, [M1, M2])] == ["plain"]


def test_deactivate_kills_slots_that_ignore_ctrl_c(root: Path) -> None:
    hc = FakeHc()
    hc.interrupt_exits = False
    mods = make(root, hc, stop_grace_s=0.05, poll_s=0.01)
    mods.activate(FLEET, [M1], "subdomains")
    assert mods.deactivate(FLEET, [M1], "subdomains") == {M1: 1}
    assert len(hc.killed) == 1 and hc.terminals == {}
