"""Start, list, inspect and destroy fleets through the Temporal client."""

from __future__ import annotations

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from warden.fleet.models import FleetSpec, FleetState, FleetStatus, fleet_workflow_id, validate_fleet_id
from warden.fleet.workflow import FleetWorkflow

MAX_INSTANCES = 20


class FleetNotFound(LookupError):
    pass


async def start_fleet(client: Client, spec: FleetSpec, task_queue: str) -> FleetStatus:
    """Start a FleetWorkflow. Starting a fleet id that is already running is a no-op."""
    validate_fleet_id(spec.fleet_id)
    if not 1 <= spec.instances <= MAX_INSTANCES:
        raise ValueError(f"instances must be between 1 and {MAX_INSTANCES}, got {spec.instances}")
    if spec.max_lifetime_min <= 0:
        raise ValueError("max_lifetime_min must be positive")
    try:
        await client.start_workflow(
            FleetWorkflow.run, spec, id=fleet_workflow_id(spec.fleet_id), task_queue=task_queue
        )
    except WorkflowAlreadyStartedError:
        pass
    return FleetStatus(fleet_id=spec.fleet_id, state=FleetState.PROVISIONING.value)


async def fleet_status(client: Client, fleet_id: str) -> FleetStatus:
    validate_fleet_id(fleet_id)
    try:
        return await client.get_workflow_handle(fleet_workflow_id(fleet_id)).query(FleetWorkflow.status)
    except RPCError as exc:
        raise FleetNotFound(f"no fleet {fleet_id!r}") from exc


async def list_fleets(client: Client) -> list[FleetStatus]:
    """Every fleet whose workflow is still running (that is, not yet DONE or FAILED)."""
    out = []
    async for wf in client.list_workflows("WorkflowType='FleetWorkflow' AND ExecutionStatus='Running'"):
        if not wf.id.startswith("fleet/"):
            continue
        try:
            out.append(await client.get_workflow_handle(wf.id).query(FleetWorkflow.status))
        except RPCError:
            continue  # finished between the listing and the query
    return sorted(out, key=lambda s: s.fleet_id)


async def destroy_fleet(client: Client, fleet_id: str, reason: str = "requested") -> FleetStatus:
    """Ask a fleet to destroy itself. A fleet that is already finished is left as it is."""
    handle = client.get_workflow_handle(fleet_workflow_id(validate_fleet_id(fleet_id)))
    try:
        desc = await handle.describe()
    except RPCError as exc:
        raise FleetNotFound(f"no fleet {fleet_id!r}") from exc
    if desc.status == WorkflowExecutionStatus.RUNNING:
        await handle.signal(FleetWorkflow.request_destroy, reason)
    return await handle.query(FleetWorkflow.status)
