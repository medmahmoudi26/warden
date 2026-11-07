"""fleet/client.py and terminate_fleet_runs against a real Temporal dev server (fake fleet activities)."""

from __future__ import annotations

import asyncio

import pytest
from temporalio import workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

from tests.fakes import FakeHc, FakeHerdr, FakeProvider, FakeTs
from tests.test_fleet_workflow import ACTIVITIES, reset
from warden.config import load_settings
from warden.fleet import client as fleets
from warden.fleet.activities import FleetActivities
from warden.fleet.models import FleetSpec, FleetState
from warden.fleet.workflow import FleetWorkflow
from warden.modules import Modules

TQ = "test-fleet-client"


@workflow.defn(name="Sleeper")
class Sleeper:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


async def until(check, timeout: float = 15.0):
    for _ in range(int(timeout / 0.1)):
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("condition never held")


async def test_start_list_status_destroy(dev_server) -> None:
    reset()
    client = dev_server.client
    async with Worker(client, task_queue=TQ, workflows=[FleetWorkflow], activities=ACTIVITIES):
        for fid in ("cccc0001", "cccc0002"):
            st = await fleets.start_fleet(client, FleetSpec(fleet_id=fid, instances=1), TQ)
            assert st.state == FleetState.PROVISIONING.value
        # starting a running fleet again is a no-op
        await fleets.start_fleet(client, FleetSpec(fleet_id="cccc0001", instances=1), TQ)

        async def both_ready():
            listed = await fleets.list_fleets(client)
            return (
                listed
                if [(f.fleet_id, f.state) for f in listed]
                == [
                    ("cccc0001", "READY"),
                    ("cccc0002", "READY"),
                ]
                else None
            )

        listed = await until(both_ready)
        assert all(f.deadline for f in listed)

        await fleets.destroy_fleet(client, "cccc0001")
        await client.get_workflow_handle("fleet/cccc0001").result()
        assert (await fleets.fleet_status(client, "cccc0001")).state == "DONE"
        assert (await fleets.fleet_status(client, "cccc0002")).state == "READY"

        async def one_left():
            ids = [f.fleet_id for f in await fleets.list_fleets(client)]
            return ids == ["cccc0002"]

        await until(one_left)
        again = await fleets.destroy_fleet(client, "cccc0001")  # already DONE: unchanged
        assert again.state == "DONE"
        await fleets.destroy_fleet(client, "cccc0002")
        await client.get_workflow_handle("fleet/cccc0002").result()


async def test_unknown_fleet_and_bad_input(dev_server) -> None:
    with pytest.raises(fleets.FleetNotFound):
        await fleets.fleet_status(dev_server.client, "nope0000")
    with pytest.raises(ValueError, match="instances"):
        await fleets.start_fleet(dev_server.client, FleetSpec(fleet_id="dddd0001", instances=0), TQ)
    with pytest.raises(ValueError, match="fleet_id"):
        await fleets.fleet_status(dev_server.client, "Bad/Id")


async def test_terminate_fleet_runs_with_real_client(dev_server) -> None:
    client = dev_server.client
    settings = load_settings({})
    hc, herdr = FakeHc(), FakeHerdr()
    acts = FleetActivities(
        settings,
        hc=hc,
        herdr=herdr,
        tailscale=FakeTs(),
        provider=FakeProvider(),
        client=client,
        modules=Modules(settings, hc, herdr),
    )
    async with Worker(client, task_queue=TQ, workflows=[Sleeper]):
        mine = await client.start_workflow("Sleeper", id="run/eeee0001/subdomains-aaaaaa", task_queue=TQ)
        other = await client.start_workflow("Sleeper", id="run/ffff0001/subdomains-bbbbbb", task_queue=TQ)

        async def visible():
            ids = [wf.id async for wf in client.list_workflows("ExecutionStatus='Running'")]
            return mine.id in ids and other.id in ids

        await until(visible)
        assert await ActivityEnvironment().run(acts.terminate_fleet_runs, "eeee0001") == 1
        desc = await mine.describe()
        assert desc.status == WorkflowExecutionStatus.TERMINATED
        events = [e async for e in mine.fetch_history_events()]
        assert events[-1].workflow_execution_terminated_event_attributes.reason == "fleet eeee0001 destroyed"
        assert (await other.describe()).status == WorkflowExecutionStatus.RUNNING
        await other.terminate()
