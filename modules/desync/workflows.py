"""DesyncScan: HTTP desync checks over a list of URLs, in host-grouped chunks across the fleet's desync workers.

Temporal hands each chunk to whichever worker is free, retries a failed chunk once on
any worker, and delivers cancellation through heartbeats. The result is counts; the
observations are files.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities import ScanChunk, scan_chunk
    from helpers import (
        BUDGET_S,
        CHECKS,
        SWEEP_BUDGET_S,
        chunk_targets,
        chunk_timeout,
        error_message,
        is_cancellation,
    )

HEARTBEAT_TIMEOUT = timedelta(minutes=2)  # a lost worker is noticed within 2 minutes; blips are survived
RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5))
COUNTS = ("probed", "skipped", "probes", "requests", "signalled", "withdrawn")


@dataclass
class DesyncInput:
    targets: list[str]
    checks: list[str] = field(default_factory=lambda: list(CHECKS))
    sweep: bool = False
    tier: int = 1
    rate_ms: int = 1000
    chunk_size: int = 20


@workflow.defn
class DesyncScan:
    @workflow.run
    async def run(self, req: DesyncInput) -> dict:
        parts = chunk_targets(req.targets, req.chunk_size)
        budget = SWEEP_BUDGET_S if req.sweep else BUDGET_S
        run_id = workflow.info().workflow_id

        results = await asyncio.gather(
            *(
                workflow.execute_activity(
                    scan_chunk,
                    ScanChunk(
                        run_id=run_id,
                        index=i + 1,
                        total=len(parts),
                        targets=part,
                        checks=list(req.checks),
                        sweep=req.sweep,
                        tier=req.tier,
                        rate_ms=req.rate_ms,
                        budget_s=budget,
                    ),
                    start_to_close_timeout=chunk_timeout(len(part), budget),
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=RETRY,
                )
                for i, part in enumerate(parts)
            ),
            return_exceptions=True,
        )

        totals = dict.fromkeys(COUNTS, 0)
        leads = {"smuggle": 0, "split": 0}
        failed = []
        for part, result in zip(parts, results, strict=True):
            if isinstance(result, BaseException):
                if is_cancellation(result):
                    raise result
                failed.append({"targets": part, "error": error_message(result)})
                continue
            for k in COUNTS:
                totals[k] += result[k]
            for k in leads:
                leads[k] += result["leads"][k]
        if parts and len(failed) == len(parts):
            raise ApplicationError(f"all {len(parts)} chunks failed; last error: {failed[-1]['error']}")

        return {
            "urls": sum(map(len, parts)),
            **totals,
            "leads": leads,
            "checks": list(req.checks),
            "sweep": req.sweep,
            "tier": req.tier,
            "rate_ms": req.rate_ms,
            "chunks": {"total": len(parts), "succeeded": len(parts) - len(failed), "failed": len(failed)},
            "failed_chunks": failed,
        }
