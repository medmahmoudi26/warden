"""Fleet lifetime workflows (control-plane queue only).

FleetWorkflow: PROVISIONING -> READY -> DESTROYING -> DONE (or FAILED).
It is destroyed on request, on its durable max-lifetime deadline, on
cancellation, or after a failure. Modules listed in the spec are activated
once the fleet is READY; their failures are reported, never fatal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

from warden.fleet.models import FleetResult, FleetSpec, FleetState, FleetStatus, MachineEntry, machine_name

with workflow.unsafe.imports_passed_through():
    from warden.fleet.activities import FleetActivities

PROVISION_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=10),
    non_retryable_error_types=["MissingCredentialsError", "ConfigError"],
)
TEARDOWN_RETRY = RetryPolicy(maximum_attempts=5, initial_interval=timedelta(seconds=10))


def _cause(exc: BaseException) -> str:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ApplicationError):
            return cur.message
        cur = cur.__cause__
    return str(exc)


@workflow.defn
class FleetWorkflow:
    def __init__(self) -> None:
        self._spec: FleetSpec | None = None
        self._state = FleetState.PROVISIONING.value
        self._machines: list[MachineEntry] = []
        self._deadline: str | None = None
        self._error: str | None = None
        self._destroy_reason: str | None = None
        self._destroy_requested = False
        self._module_errors: dict[str, str] = {}

    @workflow.signal
    def request_destroy(self, reason: str = "requested") -> None:
        if not self._destroy_requested:
            self._destroy_requested = True
            self._destroy_reason = self._destroy_reason or reason

    @workflow.query
    def status(self) -> FleetStatus:
        return FleetStatus(
            fleet_id=self._spec.fleet_id if self._spec else "",
            state=self._state,
            deadline=self._deadline,
            machines=list(self._machines),
            error=self._error,
            destroy_reason=self._destroy_reason,
            module_errors=dict(self._module_errors),
        )

    async def _teardown_step(self, fn: Callable, *args: Any) -> None:
        """Run one teardown activity. A cancellation delivered mid-teardown is
        absorbed and the activity re-issued, so teardown always completes."""
        for _ in range(3):
            try:
                await workflow.execute_activity_method(
                    fn,
                    args=list(args),
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=TEARDOWN_RETRY,
                )
                return
            except asyncio.CancelledError:
                continue
            except ActivityError as exc:
                msg = f"teardown {fn.__name__} failed: {_cause(exc)}"
                workflow.logger.error(msg)
                self._error = self._error or msg
                return

    async def _destroy(self) -> None:
        assert self._spec is not None
        self._state = FleetState.DESTROYING.value
        fid = self._spec.fleet_id
        names = [m.name for m in self._machines] or [
            machine_name(fid, i) for i in range(self._spec.instances)
        ]
        hosts = [m.tailnet_host for m in self._machines if m.tailnet_host]
        await self._teardown_step(FleetActivities.terminate_fleet_runs, fid)
        await self._teardown_step(FleetActivities.unregister_machines, fid, names)
        await self._teardown_step(FleetActivities.destroy_fleet, fid)
        await self._teardown_step(FleetActivities.delete_tailnet_devices, fid)
        if hosts:
            await self._teardown_step(FleetActivities.prune_known_hosts, hosts)

    async def _activate_modules(self, spec: FleetSpec) -> None:
        try:
            self._module_errors = await workflow.execute_activity_method(
                FleetActivities.activate_modules,
                args=[spec.fleet_id, [m.name for m in self._machines], list(spec.modules)],
                start_to_close_timeout=timedelta(minutes=20),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            if is_cancelled_exception(exc):
                raise asyncio.CancelledError from exc
            self._module_errors = {m: _cause(exc) for m in spec.modules}

    @workflow.run
    async def run(self, spec: FleetSpec) -> FleetResult:
        self._spec = spec
        deadline = workflow.now() + timedelta(minutes=spec.max_lifetime_min)
        self._deadline = deadline.isoformat()
        failed = False
        try:
            self._machines = await workflow.execute_activity_method(
                FleetActivities.provision_fleet,
                spec,
                start_to_close_timeout=timedelta(minutes=45),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=PROVISION_RETRY,
            )
            self._machines = await workflow.execute_activity_method(
                FleetActivities.wait_fleet_ready,
                args=[spec.fleet_id, self._machines, spec.ready_timeout_min * 60],
                start_to_close_timeout=timedelta(minutes=spec.ready_timeout_min + 5),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            self._state = FleetState.READY.value
            if spec.modules:
                await self._activate_modules(spec)
            remaining = (deadline - workflow.now()).total_seconds()
            try:
                if remaining <= 0:
                    raise TimeoutError
                await workflow.wait_condition(
                    lambda: self._destroy_requested, timeout=timedelta(seconds=remaining)
                )
            except TimeoutError:
                self._destroy_reason = self._destroy_reason or "max_lifetime"
        except asyncio.CancelledError:
            self._destroy_reason = self._destroy_reason or "cancelled"
        except ActivityError as exc:
            if is_cancelled_exception(exc):  # cancelled while provisioning
                self._destroy_reason = self._destroy_reason or "cancelled"
            else:
                failed = True
                self._error = _cause(exc)
                self._destroy_reason = "failed"

        await self._destroy()
        self._state = FleetState.FAILED.value if failed else FleetState.DONE.value
        return FleetResult(
            fleet_id=spec.fleet_id, state=self._state, error=self._error, destroy_reason=self._destroy_reason
        )


@workflow.defn
class SweepWorkflow:
    """One orphan sweep (started by warden-serve at startup)."""

    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity_method(
            FleetActivities.sweep_orphans,
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
