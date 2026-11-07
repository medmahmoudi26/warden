"""WebProbe: httpx over a list of hosts or URLs, split into chunks across the fleet's webs workers.

Temporal hands each chunk to whichever worker is free, retries a failed chunk
once on any worker, and delivers cancellation through heartbeats.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities import ProbeChunk, probe_chunk
    from helpers import error_message, is_cancellation

CHUNK_TIMEOUT = timedelta(minutes=60)
HEARTBEAT_TIMEOUT = timedelta(minutes=2)  # a lost worker is noticed within 2 minutes; blips are survived
RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5))


@dataclass
class WebProbeInput:
    targets: list[str]
    chunk_size: int = 200
    ports: list[int] = field(default_factory=list)


@workflow.defn
class WebProbe:
    @workflow.run
    async def run(self, req: WebProbeInput) -> dict:
        targets = list(dict.fromkeys(req.targets))
        size = max(1, req.chunk_size)
        parts = [targets[i : i + size] for i in range(0, len(targets), size)]
        run_id = workflow.info().workflow_id

        results = await asyncio.gather(
            *(
                workflow.execute_activity(
                    probe_chunk,
                    ProbeChunk(run_id=run_id, index=i + 1, total=len(parts), targets=part, ports=req.ports),
                    start_to_close_timeout=CHUNK_TIMEOUT,
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=RETRY,
                )
                for i, part in enumerate(parts)
            ),
            return_exceptions=True,
        )

        alive, failed = 0, []
        for part, result in zip(parts, results, strict=True):
            if isinstance(result, BaseException):
                if is_cancellation(result):
                    raise result
                failed.append({"targets": part, "error": error_message(result)})
            else:
                alive += result["alive"]
        if parts and len(failed) == len(parts):
            raise ApplicationError(f"all {len(parts)} chunks failed; last error: {failed[-1]['error']}")

        return {
            "targets": len(targets),
            "alive": alive,
            "ports": req.ports,
            "chunks": {"total": len(parts), "succeeded": len(parts) - len(failed), "failed": len(failed)},
            "failed_chunks": failed,
        }
