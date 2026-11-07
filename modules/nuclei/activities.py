"""scan_chunk: nuclei on one chunk of URLs (or hosts, which nuclei probes first), on the worker's machine.

nuclei runs its default template set (what it selects without template flags)
at every severity. Each finding is printed to the slot's terminal in reconftw's
format. The result carries the files nuclei.jsonl, nuclei.txt and one
nuclei-<severity>.txt per severity found, for Warden to assemble.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass

from helpers import (
    SEVERITIES,
    chunk_label,
    describe,
    output_files,
    pack,
    run_tool,
    severity,
    templates_version,
)
from temporalio import activity
from temporalio.exceptions import ApplicationError

# 150 requests/s and 2 retries (reconftw); JSON lines on stdout; scan stats in the tab every minute.
NUCLEI_FLAGS = ["-rl", "150", "-retries", "2", "-jsonl", "-nc", "-duc", "-stats", "-si", "60"]


@dataclass
class ScanChunk:
    run_id: str
    index: int
    total: int
    targets: list[str]


@activity.defn
async def scan_chunk(chunk: ScanChunk) -> dict:
    label = chunk_label(chunk.run_id, chunk.index, chunk.total, len(chunk.targets))
    print(f"\n[nuclei] >>> {label}", flush=True)
    findings: list[dict] = []

    def on_line(line: str) -> None:
        try:
            finding = json.loads(line)
        except ValueError:
            print(line, flush=True)
            return
        findings.append(finding)
        print(describe(finding), flush=True)

    with tempfile.TemporaryDirectory(prefix="nuclei-") as tmp:
        targets = os.path.join(tmp, "targets.txt")
        with open(targets, "w") as f:
            f.write("\n".join(chunk.targets) + "\n")
        try:
            rc = await run_tool(
                [os.environ.get("NUCLEI_BIN", "nuclei"), "-l", targets, *NUCLEI_FLAGS], on_line
            )
        except asyncio.CancelledError:
            print(f"[nuclei] <<< {label}: cancelled", flush=True)
            raise
    if rc != 0:
        print(f"[nuclei] <<< {label}: nuclei exited {rc}", flush=True)
        raise ApplicationError(f"nuclei exited {rc}")

    # Raw requests and responses are evidence, so keep them unless they don't fit in a payload.
    raw_dropped = False
    try:
        files = pack(output_files(findings, raw=True))
    except ApplicationError as exc:
        if exc.type != "OutputTooLarge":
            raise
        print(f"[nuclei] --- {label}: too large with raw requests and responses; dropping them", flush=True)
        files, raw_dropped = pack(output_files(findings, raw=False)), True

    print(f"[nuclei] <<< {label}: ok, {len(findings)} findings", flush=True)
    return {
        "by_severity": {s: sum(severity(f) == s for f in findings) for s in SEVERITIES},
        "templates_version": templates_version(),
        "raw_dropped": raw_dropped,
        "warden_files": files,
    }
