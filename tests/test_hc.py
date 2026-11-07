"""HcDriver against recorded hc outputs; HerdrApp against a fake herdr-app."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from warden.hc import (
    HcDriver,
    HcError,
    HerdrApp,
    OwnershipError,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "hc"
M = "wf-spike002-01"
JOB = "spiketest-shard-00-a1"  # terminal name in the recorded fixtures
ADDR = f"{M}/{JOB}"
SLOT = "subdomains-1"
SLOT_LABELS = {"warden.fleet": "spike002", "warden.module": "subdomains", "warden.slot": "1"}


def fx(name: str) -> str:
    return (FIX / name).read_text()


class FakeRunner:
    """Maps hc argv to recorded fixture output; records every call."""

    def __init__(self, overrides: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.data: list[bytes | None] = []
        self.overrides = overrides or {}

    def __call__(self, argv: list[str], timeout: float, data: bytes | None = None) -> tuple[int, str, str]:
        self.calls.append(argv)
        self.data.append(data)
        args = argv[1:]
        key = " ".join(args[:2])
        if key in self.overrides:
            rc, out = self.overrides[key]
            return rc, out, ""
        table = {
            "--version": (0, "hc 0.1.18\n"),
            "machine ls": (0, fx("machine_ls_connected.json")),
            "machine add": (0, fx("machine_add_nontemp.json")),
            "machine rm": (0, fx("machine_rm.json")),
            "run --on": (0, fx("run.json")),
            f"get {ADDR}": (0, fx("get_live.json")),
            "ls --all": (0, fx("ls_label.json")),
            f"interrupt {ADDR}": (0, fx("interrupt.json")),
            f"rm {ADDR}": (0, fx("rm.json")),
            f"kill {ADDR}": (0, fx("kill.json")),
            "exec --on": (0, fx("exec_ok.json")),
            "server status": (0, '{"version":"0.1.18"}'),
            "file write": (0, '{"written": true}'),
        }
        for prefix, (rc, out) in table.items():
            if " ".join(args).startswith(prefix):
                return rc, out, ""
        raise AssertionError(f"unexpected hc call: {argv}")


def test_version_and_machines() -> None:
    hc = HcDriver(runner=FakeRunner())
    assert hc.version() == "0.1.18"
    m = hc.machine(M)
    assert m and m["state"] == "connected" and m["version"] == "0.1.17"
    assert hc.machine("wf-nothere0-01") is None


def test_add_machine_without_temp() -> None:
    r = FakeRunner()
    out = HcDriver(runner=r).add_machine(M, f"root@{M}.tail1234.ts.net")
    assert out["temporary"] is False
    argv = r.calls[-1]
    assert "--install" in argv and "--temp" not in argv and argv[argv.index("--name") + 1] == M


def test_remove_machine_missing_is_false() -> None:
    r = FakeRunner({"machine rm": (4, fx("machine_rm_missing.json"))})
    assert HcDriver(runner=r).remove_machine(M) is False
    assert HcDriver(runner=FakeRunner()).remove_machine(M) is True


def test_run_passes_cwd_labels_and_argv() -> None:
    r = FakeRunner()
    out = HcDriver(runner=r).run(M, SLOT, SLOT_LABELS, ["bash", "-c", "./entrypoint.sh"], cwd="/opt/x")
    assert out["address"] == f"{M}/t1" and out["state"] == "live"
    argv = r.calls[-1]
    assert argv[1:8] == ["run", "--on", M, "--bg", "--no-parent", "--name", SLOT]
    assert argv[argv.index("--cwd") + 1] == "/opt/x"
    assert "warden.module=subdomains" in argv and "warden.fleet=spike002" in argv
    assert argv[argv.index("--") + 1 :] == ["bash", "-c", "./entrypoint.sh"]


def test_get_live_finished_missing() -> None:
    hc = HcDriver(runner=FakeRunner())
    live = hc.get(ADDR)
    assert live["state"] == "live" and live["labels"]["warden.job"] == JOB
    fin = HcDriver(runner=FakeRunner({f"get {ADDR}": (0, fx("get_finished.json"))})).get(ADDR)
    assert fin["state"] == "finished" and fin["signal"] == 2
    gone = HcDriver(runner=FakeRunner({f"get {ADDR}": (3, fx("get_missing.json"))})).get(ADDR)
    assert gone is None


def test_interrupt_kill_and_remove() -> None:
    hc = HcDriver(runner=FakeRunner())
    assert hc.interrupt(ADDR)["outcome"] == "exited"
    assert hc.kill(ADDR) is True
    assert hc.remove(ADDR) is True
    assert HcDriver(runner=FakeRunner({f"rm {ADDR}": (3, fx("rm_missing.json"))})).remove(ADDR) is False
    assert HcDriver(runner=FakeRunner({f"kill {ADDR}": (3, fx("kill_missing.json"))})).kill(ADDR) is False


def test_exec_decodes_base64_and_reports_code() -> None:
    ok = HcDriver(runner=FakeRunner()).exec(M, ["cloud-init", "status"])
    assert ok.code == 0 and ok.stdout == "status: done\n"
    bad = HcDriver(runner=FakeRunner({"exec --on": (0, fx("exec_fail.json"))})).exec(
        M, ["sh", "-c", "exit 3"]
    )
    assert bad.code == 3 and bad.stdout == "out\n" and bad.stderr == "err\n"
    unknown = FakeRunner({"exec --on": (4, fx("exec_unknown_machine.json"))})
    with pytest.raises(HcError) as exc:
        HcDriver(runner=unknown).exec("wf-nosuch00-01", ["true"])
    assert exc.value.kind == "machine_unknown"


def test_write_file_sends_bytes() -> None:
    r = FakeRunner()
    blob = bytes(range(256)) * 4
    HcDriver(runner=r).write_file(M, "/tmp/warden-module-x.tgz", blob)
    assert r.calls[-1][1:] == ["file", "write", "--on", M, "--json", "/tmp/warden-module-x.tgz"]
    assert r.data[-1] == blob
    with pytest.raises(OwnershipError):
        HcDriver(runner=FakeRunner()).write_file("other-machine", "/x", b"y")


def test_shell_runs_bash_snippet() -> None:
    r = FakeRunner()
    assert HcDriver(runner=r).shell(M, "ls /var/lib/warden/slots").code == 0
    assert r.calls[-1][-3:] == ["bash", "-c", "ls /var/lib/warden/slots"]


def test_listing_reports_machine_states() -> None:
    listing = json.dumps(
        {
            "terminals": [{"address": f"{M}/t1", "name": "subdomains-1", "state": "live"}],
            "machines": [
                {"name": M, "state": "connected"},
                {"name": "wf-spike002-02", "state": "connecting"},
            ],
            "hidden_finished": 0,
        }
    )
    hc = HcDriver(runner=FakeRunner({"ls --all": (0, listing)}))
    terms, machines = hc.listing({"warden.fleet": "spike002"})
    assert [t["address"] for t in terms] == [f"{M}/t1"]
    assert machines == {M: "connected", "wf-spike002-02": "connecting"}


def test_list_requires_warden_labels() -> None:
    hc = HcDriver(runner=FakeRunner())
    assert hc.list({"warden.fleet": "spike002"})[0]["address"] == f"{M}/t1"
    with pytest.raises(OwnershipError):
        hc.list({})
    with pytest.raises(OwnershipError):
        hc.list({"project": "x"})


@pytest.mark.parametrize(
    "call",
    [
        lambda hc: hc.add_machine("203.0.113.10", "203.0.113.10"),
        lambda hc: hc.remove_machine("other-machine"),
        lambda hc: hc.exec("other-machine", ["true"]),
        lambda hc: hc.interrupt("t4"),
        lambda hc: hc.remove("other-machine/t4"),
        lambda hc: hc.get("t11"),
        lambda hc: hc.kill("other-machine/t4"),
        lambda hc: hc.run(M, "my-shell", {"warden.module": "my-shell"}, ["bash"]),
        lambda hc: hc.run(M, SLOT, {}, ["bash"]),
        lambda hc: hc.run(M, SLOT, {"warden.module": "other"}, ["bash"]),
        lambda hc: hc.run(M, JOB, {"warden.module": "spiketest-shard-00"}, ["bash"]),
        lambda hc: hc.run("other-machine", SLOT, SLOT_LABELS, ["bash"]),
    ],
)
def test_ownership_guard_blocks_before_invoking_hc(call) -> None:
    r = FakeRunner()
    with pytest.raises(OwnershipError):
        call(HcDriver(runner=r))
    assert r.calls == []


# --- Herdr.app ------------------------------------------------------------------

M1 = "wf-abcd1234-01"
OPERATOR_SPACE = {
    "id": "sp-operator",
    "name": "foodsmart-mobile",
    "folder": "/Users/op",
    "machine": None,
    "tabs": [],
}


class FakeHerdrApp:
    """herdr-app CLI: `ls` and `do space.new|space.close`, with a call log."""

    def __init__(self, spaces: list | None = None, broken: bool = False) -> None:
        self.spaces = list(spaces or [dict(OPERATOR_SPACE)])
        self.broken = broken
        self.calls: list[list[str]] = []
        self._n = 0

    def __call__(self, argv: list[str], timeout: float, data: bytes | None = None) -> tuple[int, str, str]:
        self.calls.append(argv[1:])
        if self.broken:
            return 1, json.dumps({"error": "no Herdr window is running"}), ""
        args = argv[1:]
        if args == ["ls"]:
            return 0, json.dumps({"spaces": self.spaces}), ""
        if args[:2] == ["do", "space.new"]:
            opts = dict(zip(args[2::2], args[3::2], strict=True))
            self._n += 1
            sid = f"{opts['--machine']}/sp-new-{self._n}"  # ls prefixes remote ids with the machine
            self.spaces.append(
                {
                    "id": sid,
                    "name": opts["--name"],
                    "machine": opts["--machine"],
                    "folder": opts["--folder"],
                    "tabs": [],
                }
            )
            return 0, json.dumps({"space": sid}), ""
        if args[:2] == ["do", "tab.rename"]:
            tid, name = args[args.index("--id") + 1], args[args.index("--name") + 1]
            for sp in self.spaces:
                for tab in sp["tabs"]:
                    if tab["id"] == tid:
                        tab["name"] = name
            return 0, "{}", ""
        if args[:2] == ["do", "space.close"]:
            sid = args[args.index("--id") + 1]
            self.spaces = [x for x in self.spaces if x["id"] != sid]
            return 0, "{}", ""
        raise AssertionError(argv)


def test_ensure_space_creates_once_on_the_machine() -> None:
    fake = FakeHerdrApp()
    app = HerdrApp(runner=fake)
    sid = app.ensure_space(M1)
    assert sid == "sp-new-1"  # machine-local id: what a terminal on M1 must be labelled with
    assert app.ensure_space(M1) == sid  # reused, not duplicated
    created = [c for c in fake.calls if c[:2] == ["do", "space.new"]]
    assert created == [
        ["do", "space.new", "--name", f"warden {M1}", "--machine", M1, "--folder", "/root", "--tab", "shell"]
    ]
    assert fake.spaces[0] == OPERATOR_SPACE


def test_name_tab_renames_the_slots_tab() -> None:
    fake = FakeHerdrApp()
    app = HerdrApp(runner=fake)
    app.ensure_space(M1)
    space = fake.spaces[-1]
    space["tabs"] = [
        {"id": f"{M1}/tb-shell", "name": "shell", "terminals": [{"terminal": f"{M1}/t1"}]},
        {"id": f"{M1}/tb-job", "name": "2", "terminals": [{"terminal": f"{M1}/t3"}]},
    ]
    assert app.name_tab(M1, f"{M1}/t3", "subdomains #1")
    assert [t["name"] for t in space["tabs"]] == ["shell", "subdomains #1"]
    assert app.name_tab(M1, f"{M1}/t9", "nope", wait_s=0) is False


def test_close_spaces_on_only_closes_that_machines_spaces() -> None:
    other = {"id": "sp-other", "name": "warden wf-zzzz9999-01", "machine": "wf-zzzz9999-01", "tabs": []}
    fake = FakeHerdrApp([dict(OPERATOR_SPACE), other])
    app = HerdrApp(runner=fake)
    app.ensure_space(M1)
    assert app.close_spaces_on(M1) == 1
    assert [x["id"] for x in fake.spaces] == ["sp-operator", "sp-other"]
    assert app.close_spaces_on(M1) == 0  # idempotent


def test_herdr_errors_surface_as_hc_error() -> None:
    with pytest.raises(HcError, match="no Herdr window"):
        HerdrApp(runner=FakeHerdrApp(broken=True)).ensure_space(M1)


@pytest.mark.parametrize("machine", ["203.0.113.10", "other-machine", ""])
def test_herdr_refuses_non_fleet_machines(machine: str) -> None:
    fake = FakeHerdrApp()
    with pytest.raises(OwnershipError):
        HerdrApp(runner=fake).close_spaces_on(machine)
    with pytest.raises(OwnershipError):
        HerdrApp(runner=fake).ensure_space(machine)
    assert fake.calls == []


def test_prune_known_hosts(tmp_path: Path) -> None:
    import subprocess

    from warden.hc import prune_known_hosts

    kh = tmp_path / "known_hosts.warden"
    key = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], capture_output=True
    )
    assert key.returncode == 0
    pub = (tmp_path / "k.pub").read_text().split()
    kh.write_text(f"wf-abcd1234-01.tail.ts.net {pub[0]} {pub[1]}\nkeep.example.com {pub[0]} {pub[1]}\n")
    prune_known_hosts(["wf-abcd1234-01.tail.ts.net"], kh)
    text = kh.read_text()
    assert "wf-abcd1234-01" not in text and "keep.example.com" in text
    with pytest.raises(OwnershipError):
        prune_known_hosts(["keep.example.com"], kh)
