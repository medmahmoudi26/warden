"""fuzz_chunk: ffuf with the leaky-paths wordlist on one chunk of URLs, on the worker's machine.

The URLs are fuzzed one after another with reconftw's flags, as ``<url>/FUZZ``
with the wordlist's leading slashes removed. Each hit is printed
to the slot's terminal. The result carries the files ffuf.jsonl (ffuf's JSON
records) and ffuf.txt (``<status> <length> <url>``, as reconftw writes it), for
Warden to assemble.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from helpers import chunk_label, fuzz_base, pack, relative_wordlist, run_tool
from temporalio import activity
from temporalio.exceptions import ApplicationError

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# reconftw: every status but 404, auto-calibration, stop on spurious 403s, 15 minutes per URL.
FFUF_FLAGS = [
    "-mc", "all", "-fc", "404", "-ac", "-sf", "-maxtime", "900", "-t", "40",
    "-H", f"User-Agent: {USER_AGENT}", "-noninteractive", "-json",
]  # fmt: skip


@dataclass
class FuzzChunk:
    run_id: str
    index: int
    total: int
    targets: list[str]


@activity.defn
async def fuzz_chunk(chunk: FuzzChunk) -> dict:
    label = chunk_label(chunk.run_id, chunk.index, chunk.total, len(chunk.targets))
    print(f"\n[ffuf] >>> {label}", flush=True)
    ffuf = os.environ.get("FFUF_BIN", "ffuf")
    data_dir = Path(os.environ.get("WARDEN_DATA_DIR", "/opt/warden/data/ffuf"))
    jsonl, readable, errors = [], [], {}

    def on_line(line: str) -> None:
        try:
            hit = json.loads(line)
        except ValueError:
            print(line, flush=True)
            return
        if "url" in hit:
            jsonl.append(line)
            readable.append(f"{hit.get('status')} {hit.get('length')} {hit['url']}")
            print(readable[-1], flush=True)

    with tempfile.TemporaryDirectory(prefix="ffuf-") as tmp:
        wordlist = relative_wordlist(data_dir / "leaky-paths.txt", Path(tmp) / "leaky-paths.txt")
        for i, url in enumerate(chunk.targets, 1):
            base = fuzz_base(url)
            print(f"[ffuf] [{i}/{len(chunk.targets)}] {base}", flush=True)
            argv = [ffuf, "-u", f"{base}/FUZZ", "-w", str(wordlist), *FFUF_FLAGS]
            try:
                rc = await run_tool(argv, on_line)
            except asyncio.CancelledError:
                print(f"[ffuf] <<< {label}: cancelled", flush=True)
                raise
            if rc != 0:
                errors[url] = f"ffuf exited {rc}"
                print(f"[ffuf] [{i}/{len(chunk.targets)}] {base}: ffuf exited {rc}", flush=True)
    if chunk.targets and len(errors) == len(chunk.targets):
        print(f"[ffuf] <<< {label}: ffuf failed on every URL", flush=True)
        raise ApplicationError(f"ffuf failed on every URL: {next(iter(errors.values()))}")

    files = pack({"ffuf.jsonl": jsonl, "ffuf.txt": readable})
    print(f"[ffuf] <<< {label}: ok, {len(readable)} results", flush=True)
    return {"results": len(readable), "errors": errors, "warden_files": files}
