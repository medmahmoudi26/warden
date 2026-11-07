"""probe_chunk: httpx on one chunk of hosts or URLs, on the worker's machine.

Each live site is printed to the slot's terminal (its Herdr tab) as httpx finds
it. The result carries three files for Warden to assemble on the operator's
machine: webs.jsonl (httpx's JSON), webs.txt (one readable line per site) and
urls.txt (the live URLs, input of the nuclei and ffuf modules).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field

from helpers import chunk_label, describe, pack, run_tool
from temporalio import activity
from temporalio.exceptions import ApplicationError

HTTPX_FLAGS = [
    # what to record
    "-sc", "-cl", "-ct", "-location", "-rt", "-lc", "-wc", "-title", "-server", "-td",
    "-ip", "-cname", "-cdn", "-favicon", "-hash", "sha256", "-tls-grab",
    # how to probe: follow same-host redirects, 150 requests/s, 10 s timeout, 2 retries
    "-fhr", "-random-agent", "-rl", "150", "-t", "50", "-timeout", "10", "-retries", "2",
    # JSON lines on stdout
    "-j", "-silent", "-nc", "-duc",
]  # fmt: skip


@dataclass
class ProbeChunk:
    run_id: str
    index: int
    total: int
    targets: list[str]
    ports: list[int] = field(default_factory=list)


@activity.defn
async def probe_chunk(chunk: ProbeChunk) -> dict:
    label = chunk_label(chunk.run_id, chunk.index, chunk.total, len(chunk.targets))
    print(f"\n[webs] >>> {label}", flush=True)
    jsonl, readable, urls = [], [], []

    def on_line(line: str) -> None:
        try:
            site = json.loads(line)
        except ValueError:
            print(line, flush=True)
            return
        if site.get("failed") or not site.get("url"):
            return
        jsonl.append(line)
        readable.append(describe(site))
        urls.append(site["url"])
        print(readable[-1], flush=True)

    with tempfile.TemporaryDirectory(prefix="webs-") as tmp:
        targets = os.path.join(tmp, "targets.txt")
        with open(targets, "w") as f:
            f.write("\n".join(chunk.targets) + "\n")
        argv = [os.environ.get("HTTPX_BIN", "httpx"), "-l", targets, *HTTPX_FLAGS]
        if chunk.ports:
            argv += ["-p", ",".join(map(str, chunk.ports))]
        try:
            rc = await run_tool(argv, on_line)
        except asyncio.CancelledError:
            print(f"[webs] <<< {label}: cancelled", flush=True)
            raise
    if rc != 0:
        print(f"[webs] <<< {label}: httpx exited {rc}", flush=True)
        raise ApplicationError(f"httpx exited {rc}")

    files = pack({"webs.jsonl": jsonl, "webs.txt": readable, "urls.txt": urls})
    print(f"[webs] <<< {label}: ok, {len(urls)} live", flush=True)
    return {"alive": len(urls), "warden_files": files}
