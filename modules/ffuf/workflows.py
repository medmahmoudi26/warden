"""WebFuzz: ffuf with the leaky-paths wordlist over a list of URLs, split into chunks across the fleet's ffuf workers.

Temporal hands each chunk to whichever worker is free, retries a failed chunk
once on any worker, and delivers cancellation through heartbeats.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities import FuzzChunk, fuzz_chunk
    from helpers import error_message, is_cancellation

CHUNK_TIMEOUT = timedelta(hours=2)
HEARTBEAT_TIMEOUT = timedelta(minutes=2)  # a lost worker is noticed within 2 minutes; blips are survived
RETRY = RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=5))


@dataclass
class WebFuzzInput:
    targets: list[str]
    chunk_size: int = 5  # one URL can return every wordlist entry, so chunks stay small


@workflow.defn
class WebFuzz:
    @workflow.run
    async def run(self, req: WebFuzzInput) -> dict:
        targets = list(dict.fromkeys(req.targets))
        size = max(1, req.chunk_size)
        parts = [targets[i : i + size] for i in range(0, len(targets), size)]
        run_id = workflow.info().workflow_id

        results = await asyncio.gather(
            *(
                workflow.execute_activity(
                    fuzz_chunk,
                    FuzzChunk(run_id=run_id, index=i + 1, total=len(parts), targets=part),
                    start_to_close_timeout=CHUNK_TIMEOUT,
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=RETRY,
                )
                for i, part in enumerate(parts)
            ),
            return_exceptions=True,
        )

        hits, url_errors, failed = 0, {}, []
        for part, result in zip(parts, results, strict=True):
            if isinstance(result, BaseException):
                if is_cancellation(result):
                    raise result
                failed.append({"targets": part, "error": error_message(result)})
                continue
            hits += result["results"]
            url_errors.update(result["errors"])
        if parts and len(failed) == len(parts):
            raise ApplicationError(f"all {len(parts)} chunks failed; last error: {failed[-1]['error']}")

        return {
            "targets": len(targets),
            "results": hits,
            "url_errors": url_errors,
            "chunks": {"total": len(parts), "succeeded": len(parts) - len(failed), "failed": len(failed)},
            "failed_chunks": failed,
        }
