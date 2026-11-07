# /// script
# requires-python = ">=3.11"
# dependencies = ["temporalio>=1.35,<2"]
# ///
"""Temporal worker for the subdomains module, started by entrypoint.sh in a slot's terminal.

One activity at a time per slot, so each tab shows one step at a time.
Ctrl+C (``hc interrupt``) or SIGTERM stops it gracefully.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import timedelta

from activities import bruteforce, passive, permute, regex_permute, resolve
from helpers import data_converter
from temporalio.client import Client
from temporalio.worker import Worker
from workflows import DomainEnum, SubdomainEnum


async def main() -> None:
    address = os.environ["TEMPORAL_ADDRESS"]
    queue = os.environ["WARDEN_TASK_QUEUE"]
    identity = os.environ["WARDEN_WORKER_ID"]  # Warden waits for this identity to poll

    client = await Client.connect(address, identity=identity, data_converter=data_converter())
    worker = Worker(
        client,
        task_queue=queue,
        workflows=[SubdomainEnum, DomainEnum],
        activities=[passive, resolve, bruteforce, permute, regex_permute],
        max_concurrent_activities=1,
        identity=identity,
        graceful_shutdown_timeout=timedelta(seconds=5),
        # Heartbeats reach Temporal every 10 s (the SDK's default is 80% of the heartbeat
        # timeout), so a step survives tailnet blips of up to ~110 s instead of ~20 s.
        max_heartbeat_throttle_interval=timedelta(seconds=10),
        default_heartbeat_throttle_interval=timedelta(seconds=10),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    print(f"[subdomains] {identity} polling {queue} on {address}", flush=True)
    async with worker:
        await stop.wait()
        print("[subdomains] stopping...", flush=True)
    print("[subdomains] worker stopped", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
