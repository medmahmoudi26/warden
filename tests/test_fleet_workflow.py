"""FleetWorkflow with fake activities (time-skipping test server)."""

from __future__ import annotations

import asyncio

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from warden.fleet.models import FleetSpec, FleetState, MachineEntry
from warden.fleet.workflow import FleetWorkflow

TQ = "test-orch"
CALLS: list[tuple] = []
CFG: dict = {}


def reset(**cfg) -> None:
    CALLS.clear()
    CFG.clear()
    CFG.update({"ready": True, "module_errors": {}})
    CFG.update(cfg)


@activity.defn(name="provision_fleet")
async def f_provision(spec: FleetSpec) -> list[MachineEntry]:
    CALLS.append(("provision", spec.fleet_id))
    return [
        MachineEntry(name=f"wf-{spec.fleet_id}-{i + 1:02d}", tailnet_host=f"wf-{spec.fleet_id}-{i + 1:02d}.t")
        for i in range(spec.instances)
    ]


@activity.defn(name="wait_fleet_ready")
async def f_ready(fleet_id: str, machines: list[MachineEntry], timeout_s: int) -> list[MachineEntry]:
    CALLS.append(("ready", fleet_id))
    if not CFG["ready"]:
        raise ApplicationError(
            f"fleet {fleet_id} not ready: wf-{fleet_id}-01 stuck at temporal", non_retryable=True
        )
    return [MachineEntry(name=m.name, tailnet_host=m.tailnet_host, hc_version="0.1.18") for m in machines]


@activity.defn(name="activate_modules")
async def f_activate(fleet_id: str, machines: list[str], modules: list[str]) -> dict[str, str]:
    CALLS.append(("activate", fleet_id, tuple(machines), tuple(modules)))
    return dict(CFG["module_errors"])


@activity.defn(name="terminate_fleet_runs")
async def f_terminate(fleet_id: str) -> int:
    CALLS.append(("terminate_runs", fleet_id))
    return 0


@activity.defn(name="unregister_machines")
async def f_unregister(fleet_id: str, names: list[str]) -> int:
    CALLS.append(("unregister", fleet_id, tuple(names)))
    return len(names)


@activity.defn(name="destroy_fleet")
async def f_destroy(fleet_id: str) -> None:
    CALLS.append(("destroy", fleet_id))


@activity.defn(name="delete_tailnet_devices")
async def f_devices(fleet_id: str) -> int:
    CALLS.append(("devices", fleet_id))
    return 0


@activity.defn(name="prune_known_hosts")
async def f_prune(hosts: list[str]) -> None:
    CALLS.append(("prune", tuple(hosts)))


ACTIVITIES = [f_provision, f_ready, f_activate, f_terminate, f_unregister, f_destroy, f_devices, f_prune]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        async with Worker(e.client, task_queue=TQ, workflows=[FleetWorkflow], activities=ACTIVITIES):
            yield e


def teardown_order(fid: str) -> list[str]:
    return [
        c[0]
        for c in CALLS
        if len(c) > 1 and c[1] == fid and c[0] in ("terminate_runs", "unregister", "destroy", "devices")
    ]


async def start(env, fid: str, **kw):
    return await env.client.start_workflow(
        FleetWorkflow.run, FleetSpec(fleet_id=fid, instances=2, **kw), id=f"fleet/{fid}", task_queue=TQ
    )


async def wait_state(handle, state: str) -> None:
    for _ in range(300):
        st = await handle.query(FleetWorkflow.status)
        if st.state == state:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never reached {state}")


async def test_ready_then_destroy_terminates_runs_first(env) -> None:
    reset()
    h = await start(env, "aaaa0001")
    await wait_state(h, FleetState.READY.value)
    st = await h.query(FleetWorkflow.status)
    assert st.deadline and [m.hc_version for m in st.machines] == ["0.1.18", "0.1.18"]
    assert not any(c[0] == "activate" for c in CALLS)  # no modules requested
    await h.signal(FleetWorkflow.request_destroy, "operator")
    await h.signal(FleetWorkflow.request_destroy, "again")  # repeated destroy: no second teardown
    res = await h.result()
    assert res.state == "DONE" and res.destroy_reason == "operator"
    assert teardown_order("aaaa0001") == ["terminate_runs", "unregister", "destroy", "devices"]
    assert ("prune", ("wf-aaaa0001-01.t", "wf-aaaa0001-02.t")) in CALLS


async def test_modules_activated_once_ready(env) -> None:
    reset()
    h = await start(env, "aaaa0002", modules=["subdomains"])
    await wait_state(h, FleetState.READY.value)
    for _ in range(100):
        if any(c[0] == "activate" for c in CALLS):
            break
        await asyncio.sleep(0.02)
    assert ("activate", "aaaa0002", ("wf-aaaa0002-01", "wf-aaaa0002-02"), ("subdomains",)) in CALLS
    await h.signal(FleetWorkflow.request_destroy, "operator")
    await h.result()


async def test_module_activation_error_keeps_fleet_ready(env) -> None:
    reset(module_errors={"subdomains": "wf-aaaa0003-02: write failed"})
    h = await start(env, "aaaa0003", modules=["subdomains"])
    for _ in range(300):
        st = await h.query(FleetWorkflow.status)
        if st.module_errors:
            break
        await asyncio.sleep(0.02)
    assert st.state == FleetState.READY.value
    assert st.module_errors == {"subdomains": "wf-aaaa0003-02: write failed"}
    await h.signal(FleetWorkflow.request_destroy, "operator")
    assert (await h.result()).state == "DONE"


async def test_deadline_destroys(env) -> None:
    reset()
    h = await start(env, "aaaa0004", max_lifetime_min=5)
    res = await h.result()  # time-skipping fast-forwards the 5-minute timer
    assert res.state == "DONE" and res.destroy_reason == "max_lifetime"
    assert teardown_order("aaaa0004") == ["terminate_runs", "unregister", "destroy", "devices"]


async def test_cancel_destroys(env) -> None:
    reset()
    h = await start(env, "aaaa0005")
    await wait_state(h, FleetState.READY.value)
    await h.cancel()
    res = await h.result()
    assert res.destroy_reason == "cancelled" and "destroy" in teardown_order("aaaa0005")


async def test_readiness_failure_destroys(env) -> None:
    reset(ready=False)
    h = await start(env, "aaaa0006")
    res = await h.result()
    assert res.state == "FAILED" and "stuck at temporal" in (res.error or "")
    assert teardown_order("aaaa0006") == ["terminate_runs", "unregister", "destroy", "devices"]
