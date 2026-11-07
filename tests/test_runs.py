"""runs.py against a real Temporal dev server: a fake READY fleet, a stub module, a local 'module worker'."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.worker import Worker

import warden.runs as runs
from tests.test_fleet_workflow import ACTIVITIES, reset
from warden.fleet import client as fleets
from warden.fleet.models import FleetSpec
from warden.fleet.workflow import FleetWorkflow
from warden.modules import MachineOutcome, Module, SlotState

ORCH = "test-runs-orch"
FID = "ffff0001"
M1 = f"wf-{FID}-01"
QUEUE = f"{FID}.echo"


@activity.defn
async def slow() -> None:
    while True:
        activity.heartbeat()
        await asyncio.sleep(0.1)


def gz(text: str) -> str:
    return base64.b64encode(gzip.compress(text.encode())).decode()


@activity.defn
async def emit(spec: dict) -> dict:
    """Returns files the way module activities do; ``flaky`` fails the first attempt."""
    if spec.get("flaky") and activity.info().attempt == 1:
        raise RuntimeError("flaky")
    return {
        "warden_files": {n: gz(t) for n, t in spec.get("files", {}).items()},
        "n": len(spec.get("files", {})),
    }


@workflow.defn(name="EchoChild")
class EchoChild:
    @workflow.run
    async def run(self, arg: dict) -> None:
        await workflow.execute_activity(
            emit,
            arg,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        await workflow.execute_activity(
            slow, start_to_close_timeout=timedelta(minutes=5), heartbeat_timeout=timedelta(seconds=10)
        )


@workflow.defn(name="EchoWorkflow")
class Echo:
    @workflow.run
    async def run(self, arg: dict) -> dict:
        for spec in arg.get("emits", []):
            await workflow.execute_activity(
                emit,
                spec,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(milliseconds=100)),
            )
        if arg.get("child"):
            await workflow.execute_child_workflow(
                EchoChild.run, arg["child"], id=f"{workflow.info().workflow_id}/child"
            )
        if arg.get("activity"):
            await workflow.execute_activity(
                slow, start_to_close_timeout=timedelta(minutes=5), heartbeat_timeout=timedelta(seconds=10)
            )
        return {"echo": arg.get("value"), "pad": "x" * int(arg.get("big", 0))}


class StubModules:
    """Module catalog + slot state without hc: 'echo' exists; slots are whatever the test says."""

    def __init__(self, running: list[int] | None = None, exited: dict[int, int] | None = None) -> None:
        self.running = list(running or [])
        self.exited = dict(exited or {})  # slot -> rc, reported once a state check has run
        self.activated: list[tuple] = []
        self.deactivated: list[tuple] = []

    def get(self, name: str) -> Module:
        if name != "echo":
            raise LookupError(name)
        return Module("echo", Path("."))

    def state(self, fleet_id: str, machines: list[str]) -> list[SlotState]:
        return [SlotState(M1, "echo", n, "running") for n in self.running] + [
            SlotState(M1, "echo", n, "exited", rc) for n, rc in self.exited.items()
        ]

    def activate(self, fleet_id, machines, module, workers=1):
        self.activated.append((fleet_id, tuple(machines), module, workers))
        self.running = [1]
        return [MachineOutcome(M1, started=[1], shipped=True)]

    def deactivate(self, fleet_id, machines, module):
        self.deactivated.append((fleet_id, tuple(machines), module))
        return {M1: len(self.running)}


@pytest.fixture
async def world(dev_server, monkeypatch):
    monkeypatch.setattr(runs, "POLLERS_INTERVAL_S", 0.2)
    reset()
    client = dev_server.client
    orch = Worker(client, task_queue=ORCH, workflows=[FleetWorkflow], activities=ACTIVITIES)
    module_worker = Worker(
        client,
        task_queue=QUEUE,
        workflows=[Echo, EchoChild],
        activities=[slow, emit],
        identity=f"{M1}/echo-1",
        max_heartbeat_throttle_interval=timedelta(milliseconds=100),
        default_heartbeat_throttle_interval=timedelta(milliseconds=100),
    )
    async with orch, module_worker:
        await fleets.start_fleet(client, FleetSpec(fleet_id=FID, instances=1), ORCH)
        for _ in range(100):
            if (await fleets.fleet_status(client, FID)).state == "READY":
                break
            await asyncio.sleep(0.1)
        yield client
        await fleets.destroy_fleet(client, FID)


async def start(client, modules, arg, **kw) -> dict:
    return await runs.start_run(
        client, modules, fleet_id=FID, module="echo", workflow="EchoWorkflow", arg=arg, **kw
    )


async def test_start_wait_status_result(world) -> None:
    started = await start(world, StubModules([1]), {"value": "hi"})
    assert re.match(rf"^run/{FID}/echo-[0-9a-f]{{6}}$", started["run_id"])
    assert started["task_queue"] == QUEUE and started["workers"] == [f"{M1}/echo-1"]
    assert 0 < started["execution_timeout_s"] <= 120 * 60  # capped by the fleet deadline
    waited = await runs.run_wait(world, started["run_id"], 30, Path("unused"))
    assert waited == {"run_id": started["run_id"], "state": "COMPLETED", "result": {"echo": "hi", "pad": ""}}
    st = await runs.run_status(world, started["run_id"])
    assert st["state"] == "COMPLETED" and st["task_queue"] == QUEUE and st["workflow"] == "EchoWorkflow"


async def test_large_result_goes_to_a_file(world, tmp_path: Path) -> None:
    started = await start(world, StubModules([1]), {"value": "big", "big": 30_000})
    await world.get_workflow_handle(started["run_id"]).result()
    out = await runs.run_result(world, started["run_id"], tmp_path)
    assert out["size"] > 20_000 and len(out["preview"]) == 1_000
    path = Path(out["result_file"])
    assert path.name == "result.json" and path.parent.name.startswith(f"{FID}-echo-")
    assert path.parent.parent == tmp_path.resolve()
    assert json.loads(path.read_text())["pad"] == "x" * 30_000
    assert "files" not in out


async def test_files_from_chunks_are_merged_into_the_run_folder(world, tmp_path: Path) -> None:
    emits = [
        {"files": {"hosts.txt": "b.example.com\na.example.com\n", "out.jsonl": '{"i":2}\n{"i":1}\n'}},
        {"files": {"hosts.txt": "a.example.com\nc.example.com\n\n", "out.jsonl": '{"i":1}\n{"i":3}\n'}},
        {"files": {"hosts.txt": "d.example.com\n"}, "flaky": True},  # first attempt fails, counted once
    ]
    started = await start(world, StubModules([1]), {"value": "f", "emits": emits})
    out = await runs.run_wait(world, started["run_id"], 30, tmp_path)
    folder = tmp_path / started["run_id"].removeprefix("run/").replace("/", "-")
    assert (
        out["state"] == "COMPLETED" and out["result"]["echo"] == "f" and out["dir"] == str(folder.resolve())
    )
    assert (
        folder / "hosts.txt"
    ).read_text() == "a.example.com\nb.example.com\nc.example.com\nd.example.com\n"
    assert (folder / "out.jsonl").read_text() == '{"i":2}\n{"i":1}\n{"i":3}\n'  # completion order, deduped
    hosts = out["files"]["hosts.txt"]
    assert hosts["lines"] == 4 and hosts["bytes"] == (folder / "hosts.txt").stat().st_size
    assert hosts["path"] == str((folder / "hosts.txt").resolve()) and hosts["head"][0] == "a.example.com"
    assert "head" not in out["files"]["out.jsonl"]
    again = await runs.run_result(world, started["run_id"], tmp_path)  # repeat fetches rewrite the same
    assert again["files"] == out["files"]


async def test_cancelled_run_still_reports_its_finished_chunks(world, tmp_path: Path) -> None:
    arg = {"emits": [{"files": {"part.txt": "done\n"}}], "activity": True}
    started = await start(world, StubModules([1]), arg)
    rid = started["run_id"]
    for _ in range(100):
        if (await runs.run_status(world, rid))["pending_activities"] == 1:
            break
        await asyncio.sleep(0.1)
    await runs.run_cancel(world, rid)
    out = await runs.run_wait(world, rid, 30, tmp_path)
    assert out["state"] == "CANCELED" and "failure" in out
    assert out["files"]["part.txt"]["head"] == ["done"]


@pytest.mark.parametrize("name", ["../escape.txt", "result.json", "Upper.txt", ".hidden"])
async def test_bad_file_names_are_refused(world, tmp_path: Path, name: str) -> None:
    started = await start(world, StubModules([1]), {"emits": [{"files": {name: "x\n"}}]})
    await world.get_workflow_handle(started["run_id"]).result()
    with pytest.raises(runs.RunError, match="invalid run file name"):
        await runs.run_result(world, started["run_id"], tmp_path)
    assert not list(tmp_path.rglob("escape.txt"))


async def test_from_run_reads_a_completed_runs_file(world, tmp_path: Path) -> None:
    started = await start(
        world, StubModules([1]), {"emits": [{"files": {"hosts.txt": "b.x.com\na.x.com\n"}}]}
    )
    rid = started["run_id"]
    await world.get_workflow_handle(rid).result()
    # assembles the folder when it is missing
    assert await runs.run_targets(world, rid, {"echo": "hosts.txt"}, tmp_path, tool="t") == [
        "a.x.com",
        "b.x.com",
    ]
    with pytest.raises(runs.RunError, match="t takes its targets from a webs run, not a echo run"):
        await runs.run_targets(world, rid, {"webs": "urls.txt"}, tmp_path, tool="t")
    with pytest.raises(runs.RunError, match="produced no urls.txt"):
        await runs.run_targets(world, rid, {"echo": "urls.txt"}, tmp_path, tool="t")
    running = await start(world, StubModules([1]), {"activity": True})
    with pytest.raises(runs.RunError, match="is RUNNING, not COMPLETED"):
        await runs.run_targets(world, running["run_id"], {"echo": "hosts.txt"}, tmp_path, tool="t")
    await runs.run_cancel(world, running["run_id"])


async def test_exited_slot_fails_the_wait_at_once(world, monkeypatch) -> None:
    monkeypatch.setattr(runs, "SLOT_CHECK_EVERY_S", 0.2)
    mods = StubModules([1, 2])

    def state(fleet_id, machines):  # slot 2 never polls: its install failed with rc=1
        return [SlotState(M1, "echo", 1, "running"), SlotState(M1, "echo", 2, "exited", 1)]

    calls = {"n": 0}

    def running_first(fleet_id, machines):  # ensure_active sees both running, then slot 2 exits
        calls["n"] += 1
        return (
            [SlotState(M1, "echo", n, "running") for n in (1, 2)]
            if calls["n"] == 1
            else state(fleet_id, machines)
        )

    mods.state = running_first  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(runs.RunError, match=rf"exited before polling.*{M1}/echo-2 rc=1"):
        await start(world, mods, {"value": "x"}, pollers_timeout_s=60)
    assert loop.time() - t0 < 10


async def test_running_status_cancel_and_wait_timeout(world) -> None:
    started = await start(world, StubModules([1]), {"activity": True})
    rid = started["run_id"]
    for _ in range(100):
        st = await runs.run_status(world, rid)
        if st["pending_activities"] == 1:
            break
        await asyncio.sleep(0.1)
    assert st["state"] == "RUNNING" and st["pending_activities"] == 1
    with pytest.raises(runs.RunError, match="still RUNNING"):
        await runs.run_result(world, rid, Path("unused"))
    assert (await runs.run_wait(world, rid, 1, Path("unused")))["state"] == "RUNNING"
    cancelled = await runs.run_cancel(world, rid)
    assert cancelled["cancel_requested"] is True
    final = await runs.run_wait(world, rid, 30, Path("unused"))
    assert final["state"] == "CANCELED" and "cancel" in final["failure"].lower()
    again = await runs.run_cancel(world, rid)  # finished: unchanged
    assert again["state"] == "CANCELED" and "cancel_requested" not in again


@pytest.mark.parametrize("bad", ["fleet/ffff0001", "sweep/2026110612", "run/ffff0001", ""])
async def test_only_run_ids_are_accepted(world, bad: str) -> None:
    for call in (runs.run_status, runs.run_cancel):
        with pytest.raises(runs.RunError, match="only run ids"):
            await call(world, bad)


async def test_missing_poller_fails_naming_the_slot(world) -> None:
    with pytest.raises(runs.RunError, match=rf"{M1}/echo-2"):
        await start(world, StubModules([1, 2]), {"value": "x"}, pollers_timeout_s=1)
    assert not [
        wf async for wf in world.list_workflows("ExecutionStatus='Running'") if wf.id.startswith("run/")
    ]


async def test_module_activated_when_nothing_runs(world) -> None:
    mods = StubModules([])
    started = await start(world, mods, {"value": "x"})
    assert mods.activated == [(FID, (M1,), "echo", 1)] and started["workers"] == [f"{M1}/echo-1"]


async def test_fleet_selection(world) -> None:
    assert (await runs.resolve_fleet(world, None)).fleet_id == FID
    await fleets.start_fleet(world, FleetSpec(fleet_id="ffff0002", instances=1), ORCH)
    for _ in range(100):
        ready = [f for f in await fleets.list_fleets(world) if f.state == "READY"]
        if len(ready) == 2:
            break
        await asyncio.sleep(0.1)
    with pytest.raises(runs.RunError, match="several READY fleets.*ffff0001.*ffff0002"):
        await runs.resolve_fleet(world, None)
    await fleets.destroy_fleet(world, "ffff0002")
    await world.get_workflow_handle("fleet/ffff0002").result()
    with pytest.raises(runs.RunError, match="ffff0002 is DONE, not READY"):
        await runs.resolve_fleet(world, "ffff0002")


async def test_deactivate_refuses_in_flight_runs_unless_forced(world, monkeypatch) -> None:
    monkeypatch.setattr(runs, "CANCEL_GRACE_S", 20)
    mods = StubModules([1])
    started = await start(world, mods, {"activity": True})
    fleet = await fleets.fleet_status(world, FID)
    with pytest.raises(runs.RunError, match=re.escape(started["run_id"])):
        await runs.deactivate_module(world, mods, fleet, "echo")
    assert mods.deactivated == []
    out = await runs.deactivate_module(world, mods, fleet, "echo", force=True)
    assert out["cancelled_runs"] == [started["run_id"]] and mods.deactivated == [(FID, (M1,), "echo")]
    assert (await runs.run_status(world, started["run_id"]))["state"] == "CANCELED"


async def test_child_workflows_files_status_and_guards(world, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(runs, "CANCEL_GRACE_S", 20)
    mods = StubModules([1])
    started = await start(world, mods, {"child": {"files": {"part.txt": "from the child\n"}}})
    rid = started["run_id"]
    for _ in range(100):
        st = await runs.run_status(world, rid)
        if st["pending_children"] == 1:
            break
        await asyncio.sleep(0.1)
    assert st["pending_children"] == 1
    # the child is running too, but only the run itself counts as in flight
    assert await runs.runs_in_flight(world, f"run/{FID}/echo-") == [rid]
    await runs.run_cancel(world, rid)
    out = await runs.run_wait(world, rid, 30, tmp_path)
    assert out["state"] == "CANCELED" and out["files"]["part.txt"]["head"] == ["from the child"]
