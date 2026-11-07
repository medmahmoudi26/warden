"""Helpers for the nuclei module's workflow and activity: running tools, packing files, describing findings."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import gzip
import json
import os
import signal
import zlib
from collections.abc import Callable, Sequence

import temporalio.converter
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

HEARTBEAT_EVERY_S = 10.0
MAX_OUTPUT_BYTES = 1_500_000  # Temporal rejects payloads over 2 MB
SEVERITIES = ("critical", "high", "medium", "low", "info", "unknown")
RAW_FIELDS = ("request", "response", "curl-command")


# --- activity side ---------------------------------------------------------------------


async def run_tool(argv: list[str], on_line: Callable[[str], None]) -> int:
    """Run a tool, passing each stdout line to ``on_line`` and heartbeating until it exits.

    The tool gets its own process group: Ctrl+C in the tab does not reach it, and
    a cancelled activity kills the whole group. stderr goes to the tab.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=64 * 1024 * 1024,  # findings with raw responses make long lines
    )

    async def read() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            on_line(raw.decode("utf-8", errors="replace").rstrip("\n"))

    reader = asyncio.create_task(read())
    try:
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(reader), HEARTBEAT_EVERY_S)
                break
            except TimeoutError:
                activity.heartbeat()
        return await proc.wait()
    except asyncio.CancelledError:
        reader.cancel()
        await kill(proc)
        raise


async def kill(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the tool's process group, then SIGKILL it if it lingers."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 5)
            return
        except TimeoutError:
            pass


def pack(files: dict[str, list[str]]) -> dict[str, str]:
    """Files for Warden: name -> base64 of the gzipped lines, empty files left out."""
    packed = {
        name: base64.b64encode(gzip.compress("".join(f"{line}\n" for line in lines).encode())).decode()
        for name, lines in files.items()
        if lines
    }
    size = sum(map(len, packed.values()))
    if size > MAX_OUTPUT_BYTES:
        raise ApplicationError(
            f"chunk output too large ({size} bytes compressed; Temporal payloads are limited to 2 MB): "
            "use a smaller chunk_size",
            type="OutputTooLarge",
            non_retryable=True,
        )
    return packed


def chunk_label(run_id: str, index: int, total: int, targets: int) -> str:
    """How a chunk is named in the tab."""
    return f"{run_id} chunk {index}/{total} ({targets} targets, attempt {activity.info().attempt})"


def severity(finding: dict) -> str:
    value = str((finding.get("info") or {}).get("severity") or "unknown").lower()
    return value if value in SEVERITIES else "unknown"


def describe(finding: dict) -> str:
    """reconftw's line: ``[<template-id>[:<matcher>]] [<type>] [<severity>] <matched-at> [<extracted JSON>]``"""
    template = finding.get("template-id", "")
    if finding.get("matcher-name"):
        template += f":{finding['matcher-name']}"
    target = finding.get("matched-at") or finding.get("host", "")
    line = f"[{template}] [{finding.get('type', '')}] [{severity(finding)}] {target}"
    if finding.get("extracted-results") is not None:
        line += " " + json.dumps(finding["extracted-results"], separators=(",", ":"))
    return line


def output_files(findings: list[dict], raw: bool) -> dict[str, list[str]]:
    """nuclei.jsonl, nuclei.txt and nuclei-<severity>.txt; without raw requests and responses unless ``raw``."""

    def jsonl(finding: dict) -> str:
        kept = finding if raw else {k: v for k, v in finding.items() if k not in RAW_FIELDS}
        return json.dumps(kept, separators=(",", ":"))

    files = {"nuclei.jsonl": [jsonl(f) for f in findings], "nuclei.txt": [describe(f) for f in findings]}
    for s in SEVERITIES:
        files[f"nuclei-{s}.txt"] = [describe(f) for f in findings if severity(f) == s]
    return files


def templates_version() -> str:
    """Written by entrypoint.sh after updating the templates."""
    path = os.path.join(os.environ.get("WARDEN_DATA_DIR", "/opt/warden/data/nuclei"), "templates-version")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


# --- workflow side ---------------------------------------------------------------------


def is_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.CancelledError) or (
        isinstance(exc, ActivityError) and is_cancelled_exception(exc)
    )


def error_message(exc: BaseException) -> str:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ApplicationError):
            return cause.message
        cause = cause.__cause__
    return str(exc)


# --- payloads ----------------------------------------------------------------------------


class ZlibCodec(PayloadCodec):
    """zlib for every Temporal payload, the same codec as Warden's (src/warden/codec.py)."""

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(metadata={"encoding": b"binary/zlib"}, data=zlib.compress(p.SerializeToString()))
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload.FromString(zlib.decompress(p.data)) if p.metadata.get("encoding") == b"binary/zlib" else p
            for p in payloads
        ]


def data_converter() -> DataConverter:
    """Temporal's default converter with zlib-compressed payloads (Warden uses the same)."""
    return dataclasses.replace(temporalio.converter.default(), payload_codec=ZlibCodec())
