"""Fleet activities: provision, readiness gate, module activation, teardown, orphan sweep.

All of them are methods of :class:`FleetActivities`, which receives its
collaborators (settings, hc, Herdr.app, Tailscale API, Pulumi provider,
Temporal client, modules) once, in ``warden-serve``. Tests pass fakes.

Teardown activities are idempotent, and only Warden-owned resources are ever
touched: ``wf-*`` machines, ``warden``-tagged droplets, ``tag:warden-worker``
devices, and workflows whose id starts with ``run/<fleet_id>/``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from warden.config import ConfigError, Settings
from warden.fleet.models import FleetSpec, MachineEntry, fleet_of, fleet_run_prefix
from warden.fleet.provider import do_api
from warden.hc import prune_known_hosts

POLL_S = 10.0
TEMPORAL_PROBE_S = 5


async def heartbeating(fn: Callable[[], Any], detail: dict, every: float = 20.0) -> Any:
    """Run a blocking call in a thread, heartbeating until it returns."""
    task = asyncio.ensure_future(asyncio.to_thread(fn))
    while True:
        done, _ = await asyncio.wait({task}, timeout=every)
        if done:
            return task.result()
        activity.heartbeat(detail)


class FleetActivities:
    def __init__(
        self,
        settings: Settings,
        *,
        hc: Any,
        herdr: Any,
        tailscale: Any,
        provider: Any,
        client: Any,
        modules: Any,
    ) -> None:
        self.settings = settings
        self.hc = hc
        self.herdr = herdr
        self.tailscale = tailscale
        self.provider = provider
        self.client = client
        self.modules = modules

    def all(self) -> list[Callable]:
        """Every activity, for ``Worker(activities=...)``."""
        return [
            self.provision_fleet,
            self.wait_fleet_ready,
            self.activate_modules,
            self.terminate_fleet_runs,
            self.unregister_machines,
            self.destroy_fleet,
            self.delete_tailnet_devices,
            self.prune_known_hosts,
            self.sweep_orphans,
        ]

    # --- provision -------------------------------------------------------------

    @activity.defn
    async def provision_fleet(self, spec: FleetSpec) -> list[MachineEntry]:
        """``pulumi up``. Missing credentials raise MissingCredentialsError (non-retryable)."""
        self.settings.require_do_token()
        self.settings.require_tailscale_oauth()
        return await heartbeating(lambda: self.provider.up(spec), {"phase": "pulumi up"})

    # --- readiness -------------------------------------------------------------

    @activity.defn
    async def wait_fleet_ready(
        self, fleet_id: str, machines: list[MachineEntry], timeout_s: int
    ) -> list[MachineEntry]:
        """Readiness gate: tailnet, hc registration, boot, Herdr space (best effort), Temporal probe."""
        try:
            temporal = self.settings.fleet_temporal_address
        except ConfigError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        deadline = time.monotonic() + timeout_s
        stages: dict[str, str] = {m.name: "pending" for m in machines}
        results = await asyncio.gather(
            *(self._ready_one(m, temporal, deadline, stages) for m in machines), return_exceptions=True
        )
        failures = []
        for m, r in zip(machines, results, strict=True):
            if isinstance(r, ApplicationError):
                raise r
            if isinstance(r, BaseException):
                stage = stages.get(m.name, "?")
                hint = f" (Temporal address {temporal})" if stage == "temporal" else ""
                failures.append(f"{m.name} stuck at {stage}{hint}")
        if failures:
            raise ApplicationError(
                f"fleet {fleet_id} not ready within {timeout_s}s: " + "; ".join(failures), non_retryable=True
            )
        return list(results)  # type: ignore[arg-type]

    async def _ready_one(
        self, machine: MachineEntry, temporal: str, deadline: float, stages: dict[str, str]
    ) -> MachineEntry:
        name = machine.name
        host = machine.tailnet_host or self.settings.tailnet_host(name)
        hc = self.hc

        async def until(stage: str, check: Callable[[], Awaitable[bool]]) -> None:
            stages[name] = stage
            while True:
                if await check():
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError(stage)
                activity.heartbeat({"machine": name, "stage": stage})
                await asyncio.sleep(POLL_S)

        async def online() -> bool:
            try:
                devs = await asyncio.to_thread(
                    self.tailscale.devices, tag=self.settings.ts_tag, hostname_prefix=name
                )
            except Exception as exc:  # noqa: BLE001 — fall through to registration
                activity.logger.warning("tailscale api: %s", exc)
                return True
            return any(d.hostname == name and d.online for d in devs)

        async def registered() -> bool:
            try:
                await asyncio.to_thread(hc.add_machine, name, f"root@{host}")
                return True
            except Exception as exc:  # noqa: BLE001 — SSH not up yet
                activity.logger.info("hc machine add %s: %s", name, exc)
                return False

        info: dict = {}

        async def connected() -> bool:
            nonlocal info
            info = await asyncio.to_thread(hc.machine, name) or {}
            return info.get("state") == "connected"

        async def booted() -> bool:
            r = await asyncio.to_thread(hc.exec, name, ["cloud-init", "status"], 30)
            if "status: error" in r.stdout:
                raise ApplicationError(
                    f"{name}: cloud-init failed (debug with: ssh root@{host} cloud-init status --long)",
                    non_retryable=True,
                )
            return "status: done" in r.stdout

        async def reaches_temporal() -> bool:
            target_host, _, port = temporal.rpartition(":")
            probe = f"timeout {TEMPORAL_PROBE_S} bash -c '</dev/tcp/{target_host}/{port}'"
            r = await asyncio.to_thread(hc.shell, name, probe, TEMPORAL_PROBE_S + 25)
            return r.code == 0

        await until("tailnet", online)
        await until("register", registered)
        await until("connected", connected)
        local = await asyncio.to_thread(hc.version)
        remote = str(info.get("version", ""))
        if remote and local and remote != local:
            activity.logger.warning("%s runs hc %s, control plane runs %s", name, remote, local)
        await until("boot", booted)
        stages[name] = "space"
        try:
            await asyncio.to_thread(self.herdr.ensure_space, name)
        except Exception as exc:  # noqa: BLE001 — the Herdr window is best effort
            activity.logger.warning("herdr: no space on %s yet: %s", name, exc)
        await until("temporal", reaches_temporal)
        stages[name] = "ready"
        return MachineEntry(name=name, tailnet_host=host, public_ip=machine.public_ip, hc_version=remote)

    # --- modules ---------------------------------------------------------------

    @activity.defn
    async def activate_modules(
        self, fleet_id: str, machines: list[str], modules: list[str]
    ) -> dict[str, str]:
        """Activate each module with one worker. Errors are returned per module, never raised."""
        errors: dict[str, str] = {}
        for module in modules:
            try:
                outcomes = await heartbeating(
                    lambda module=module: self.modules.activate(fleet_id, machines, module, 1),
                    {"module": module},
                )
            except Exception as exc:  # noqa: BLE001 — reported in fleet status
                errors[module] = str(exc)
                continue
            failed = [f"{o.machine}: {o.error}" for o in outcomes if o.error]
            if failed:
                errors[module] = "; ".join(failed)
        return errors

    # --- teardown --------------------------------------------------------------

    @activity.defn
    async def terminate_fleet_runs(self, fleet_id: str) -> int:
        """Terminate the still-running runs Warden started on this fleet (``run/<fleet_id>/<module>-<id>``).

        A run's child workflows (``run/<fleet_id>/<module>-<id>/...``) are terminated with it.
        """
        prefix = fleet_run_prefix(fleet_id)
        terminated = 0
        async for wf in self.client.list_workflows("ExecutionStatus='Running'"):
            if wf.id.startswith(prefix) and "/" not in wf.id[len(prefix) :]:
                await self.client.get_workflow_handle(wf.id).terminate(reason=f"fleet {fleet_id} destroyed")
                terminated += 1
        return terminated

    @activity.defn
    async def unregister_machines(self, fleet_id: str, names: list[str]) -> int:
        """Close the machines' spaces in Herdr.app (ending their terminals), then forget the hc machines."""
        removed = 0
        for name in names:
            try:
                await asyncio.to_thread(self.herdr.close_spaces_on, name)
            except Exception as exc:  # noqa: BLE001 — the Herdr window is best effort
                activity.logger.warning("herdr: closing spaces on %s: %s", name, exc)
            if await asyncio.to_thread(self.hc.remove_machine, name):
                removed += 1
        return removed

    @activity.defn
    async def destroy_fleet(self, fleet_id: str) -> None:
        """``pulumi destroy``. A missing stack is success."""
        await heartbeating(lambda: self.provider.destroy(fleet_id), {"phase": "pulumi destroy"})

    @activity.defn
    async def delete_tailnet_devices(self, fleet_id: str) -> int:
        devs = await asyncio.to_thread(
            self.tailscale.devices, tag=self.settings.ts_tag, hostname_prefix=f"wf-{fleet_id}-"
        )
        deleted = 0
        for d in devs:
            if await asyncio.to_thread(self.tailscale.delete_device, d.id):
                deleted += 1
        return deleted

    @activity.defn
    async def prune_known_hosts(self, hosts: list[str]) -> None:
        await asyncio.to_thread(prune_known_hosts, hosts, self.settings.known_hosts_file)

    # --- sweep -----------------------------------------------------------------

    @activity.defn
    async def sweep_orphans(self) -> dict[str, Any]:
        """Destroy Warden resources that belong to no running FleetWorkflow."""
        s = self.settings
        running: set[str] = set()
        async for wf in self.client.list_workflows(
            "WorkflowType='FleetWorkflow' AND ExecutionStatus='Running'"
        ):
            if wf.id.startswith("fleet/"):
                running.add(wf.id.split("/", 1)[1])

        found: dict[str, dict[str, list]] = {}

        def note(fid: str | None, kind: str, item: Any) -> None:
            if fid:
                found.setdefault(fid, {"droplets": [], "devices": [], "machines": []})[kind].append(item)

        droplets = await asyncio.to_thread(do_api, s, "GET", "/v2/droplets?tag_name=warden&per_page=200")
        for d in droplets.get("droplets", []):
            note(fleet_of(d.get("name", "")), "droplets", d)
        for dev in await asyncio.to_thread(self.tailscale.devices, tag=s.ts_tag):
            note(fleet_of(dev.hostname), "devices", dev)
        for m in await asyncio.to_thread(self.hc.machines):
            note(fleet_of(m.get("name", "")), "machines", m)

        orphans = sorted(fid for fid in found if fid not in running)
        for fid in orphans:
            activity.heartbeat({"sweeping": fid})
            res = found[fid]
            if self.provider.has_stack(fid):
                try:
                    await asyncio.to_thread(self.provider.destroy, fid)
                except Exception as exc:  # noqa: BLE001 — fall back to direct deletes below
                    activity.logger.warning("sweep: pulumi destroy %s failed: %s", fid, exc)
            if res["droplets"]:
                await asyncio.to_thread(do_api, s, "DELETE", f"/v2/droplets?tag_name=fleet:{fid}")
                fws = await asyncio.to_thread(do_api, s, "GET", "/v2/firewalls?per_page=200")
                for fw in fws.get("firewalls", []):
                    if fw.get("name") == f"wf-{fid}":
                        await asyncio.to_thread(do_api, s, "DELETE", f"/v2/firewalls/{fw['id']}")
            for dev in res["devices"]:
                await asyncio.to_thread(self.tailscale.delete_device, dev.id)
            for m in res["machines"]:
                try:
                    await asyncio.to_thread(self.herdr.close_spaces_on, m["name"])
                except Exception:  # noqa: BLE001 — the Herdr window is best effort
                    pass
                await asyncio.to_thread(self.hc.remove_machine, m["name"])
            activity.logger.warning(
                "sweep: removed orphaned fleet %s (%d droplets, %d devices, %d hc machines)",
                fid,
                len(res["droplets"]),
                len(res["devices"]),
                len(res["machines"]),
            )
        return {"running": sorted(running), "orphans": orphans}
