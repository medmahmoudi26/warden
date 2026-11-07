"""``warden-serve``: the control plane in one process.

Startup checks, then the control-plane Temporal worker (fleet and sweep
workflows) and the MCP server, sharing one event loop and one Temporal client.
An orphan sweep runs once at startup. Module workflows do not run here: they
run on the fleet, in each module's own workers.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
from datetime import UTC, datetime

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from warden.codec import data_converter
from warden.config import Settings, load_settings
from warden.fleet.activities import FleetActivities
from warden.fleet.provider import DigitalOceanProvider
from warden.fleet.workflow import FleetWorkflow, SweepWorkflow
from warden.hc import HcDriver, HerdrApp
from warden.mcp import App, build_mcp
from warden.modules import Modules
from warden.tailscale import TailscaleAPI

logger = logging.getLogger("warden.serve")


class StartupError(RuntimeError):
    """A prerequisite of the control plane is missing; the message says how to fix it."""


async def check_temporal(settings: Settings) -> Client:
    try:
        return await Client.connect(settings.temporal_host, data_converter=data_converter())
    except Exception as exc:  # noqa: BLE001
        raise StartupError(
            f"Temporal is not reachable at {settings.temporal_host} ({exc}). "
            "Start it with: temporal server start-dev --db-filename .temporal/dev.db"
        ) from exc


def check_hc(hc: HcDriver) -> str:
    try:
        version = hc.version()
        hc.server_status()
        return version
    except Exception as exc:  # noqa: BLE001
        raise StartupError(
            f"hc (Herdr) is not available ({exc}). Open Herdr.app so `hc server status` works."
        ) from exc


def check_pulumi() -> str:
    """The Pulumi Automation API shells out to the `pulumi` CLI; find it before the first fleet does."""
    path = shutil.which("pulumi")
    if not path:
        raise StartupError(
            "pulumi CLI not found on PATH. Install it (curl -fsSL https://get.pulumi.com | sh) "
            "or start warden-serve with ~/.pulumi/bin on PATH."
        )
    return path


def check_tailscale(settings: Settings) -> TailscaleAPI:
    try:
        api = TailscaleAPI(settings)
        api.token()
        return api
    except Exception as exc:  # noqa: BLE001
        raise StartupError(
            f"Tailscale API login failed ({exc}). Check TAILSCALE_OAUTH_CLIENT_ID / _SECRET in .env."
        ) from exc


async def start_sweep(client: Client, settings: Settings) -> None:
    sweep_id = "sweep/" + datetime.now(UTC).strftime("%Y%m%d%H")
    try:
        await client.start_workflow(
            SweepWorkflow.run,
            id=sweep_id,
            task_queue=settings.orchestrator_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
        )
        logger.info("orphan sweep started (%s)", sweep_id)
    except WorkflowAlreadyStartedError:
        logger.info("orphan sweep %s already ran this hour", sweep_id)


async def run_control_plane(
    settings: Settings, *, transport: str, host: str, port: int, sweep: bool = True
) -> None:
    hc, herdr = HcDriver(settings.hc_bin), HerdrApp(settings.herdr_app_bin)
    client = await check_temporal(settings)
    version = await asyncio.to_thread(check_hc, hc)
    pulumi = check_pulumi()
    tailscale = await asyncio.to_thread(check_tailscale, settings)
    modules = Modules(settings, hc, herdr)
    found = [m.name for m in modules.list()]
    if not found:
        logger.warning("no modules found in %s (WARDEN_MODULES_DIR)", settings.modules_dir)
    logger.info(
        "checks ok: temporal=%s hc=%s pulumi=%s modules=%s", settings.temporal_host, version, pulumi, found
    )

    activities = FleetActivities(
        settings,
        hc=hc,
        herdr=herdr,
        tailscale=tailscale,
        provider=DigitalOceanProvider(settings),
        client=client,
        modules=modules,
    )
    worker = Worker(
        client,
        task_queue=settings.orchestrator_queue,
        workflows=[FleetWorkflow, SweepWorkflow],
        activities=activities.all(),
    )
    if sweep:
        await start_sweep(client, settings)

    mcp = build_mcp(App(settings=settings, client=client, herdr=herdr, modules=modules))
    kwargs = {} if transport == "stdio" else {"host": host, "port": port}
    async with worker:
        logger.info("worker on %s; MCP over %s", settings.orchestrator_queue, transport)
        await mcp.run_async(transport=transport, show_banner=False, **kwargs)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Warden control plane (Temporal worker + MCP)")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--no-sweep", action="store_true", help="skip the startup orphan sweep")
    args = parser.parse_args()
    try:
        asyncio.run(
            run_control_plane(
                load_settings(),
                transport=args.transport,
                host=args.host,
                port=args.port,
                sweep=not args.no_sweep,
            )
        )
    except StartupError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
