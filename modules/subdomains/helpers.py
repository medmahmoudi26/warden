"""Helpers for the subdomains module's workflows and activities: running tools, resolving, scoping, packing."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import gzip
import json
import os
import re
import signal
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import temporalio.converter
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError, is_cancelled_exception

HEARTBEAT_EVERY_S = 10.0
MAX_RESULT_BYTES = 1_500_000  # Temporal rejects payloads over 2 MB
PUREDNS_RATES = [
    "-l",
    "5000",
    "--rate-limit-trusted",
    "400",
    "--wildcard-tests",
    "30",
    "--wildcard-batch",
    "1500000",
]
THC_URL = "https://ip.thc.org/sb/{domain}?l=100&nocolor=1"
NAME_RE = re.compile(r"^([a-z0-9_-]+\.)+[a-z]+$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# --- tools -------------------------------------------------------------------------------


async def puredns_resolve(domain: str, step: str, candidates: Path, workdir: Path) -> set[str]:
    """The candidates that resolve (public resolvers, trusted validation, wildcards filtered)."""
    out = workdir / "resolved.txt"
    await run_step(
        domain,
        step,
        "puredns resolve",
        [tool("puredns"), "resolve", str(candidates), "-w", str(out), *resolvers()],
    )
    return in_scope(domain, read_lines(out))


async def thc(domain: str) -> list[str]:
    """reconftw's extra passive source: the first page of ip.thc.org. Failures are ignored."""
    url = os.environ.get("THC_URL", THC_URL)
    if not url:
        return []

    def fetch() -> str:
        with urllib.request.urlopen(url.format(domain=domain), timeout=30) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")

    try:
        text = await asyncio.to_thread(fetch)
    except Exception as exc:  # noqa: BLE001
        say(domain, "passive", f"ip.thc.org failed ({exc}); ignored")
        return []
    return [ANSI_RE.sub("", line) for line in text.splitlines() if not line.startswith(";;")]


async def run_step(
    domain: str, step: str, name: str, argv: list[str], on_line: Callable[[str], bool] | None = None
) -> None:
    """Run a step's tool; a non-zero exit fails the activity (and Temporal retries it)."""
    rc = await run_tool(argv, on_line=on_line)
    if rc != 0:
        say(domain, step, f"{name} exited {rc}")
        raise ApplicationError(f"{name} exited {rc} on {domain}")


async def run_tool(
    argv: list[str], on_line: Callable[[str], bool] | None = None, cwd: str | None = None
) -> int:
    """Run a tool and return its exit code, heartbeating until it exits.

    Without ``on_line`` its stdout goes to the tab; with it, each stdout line is
    passed to ``on_line``, which stops the tool by returning False (counted as
    success, like ``| head``). The tool gets its own process group: Ctrl+C in the
    tab does not reach it, and a cancelled activity kills the whole group.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE if on_line else None,
        cwd=cwd,
        start_new_session=True,
        limit=16 * 1024 * 1024,
    )
    stopped = False

    async def read() -> None:
        nonlocal stopped
        assert proc.stdout is not None and on_line is not None
        async for raw in proc.stdout:
            if on_line(raw.decode("utf-8", errors="replace").rstrip("\n")) is False:
                stopped = True
                await kill(proc)
                return

    running = asyncio.create_task(read() if on_line else proc.wait())
    try:
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(running), HEARTBEAT_EVERY_S)
                break
            except TimeoutError:
                activity.heartbeat()
        rc = await proc.wait()
        return 0 if stopped else rc
    except asyncio.CancelledError:
        running.cancel()
        await kill(proc)
        print(
            f"[subdomains] {activity.info().workflow_id}: {activity.info().activity_type} cancelled",
            flush=True,
        )
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


def regulator_command() -> tuple[list[str], str | None]:
    """regulator's command line and working folder (its venv, installed by entrypoint.sh)."""
    if os.environ.get("REGULATOR_BIN"):
        return [os.environ["REGULATOR_BIN"]], None
    home = data_dir() / "regulator"
    return [str(home / ".venv" / "bin" / "python"), "main.py"], str(home)


# --- names and files -----------------------------------------------------------------


def warden_file(name: str, names: Iterable[str]) -> dict[str, str]:
    """Names as a file for Warden (``{name: base64 of the gzipped lines}``), empty when there are none.

    The names also travel in the same result as a JSON list; the payload codec
    compresses both, and together they must fit in one payload.
    """
    lines = sorted(names)
    if not lines:
        return {}
    data = base64.b64encode(gzip.compress("".join(f"{n}\n" for n in lines).encode())).decode()
    size = len(zlib.compress(json.dumps(lines).encode())) + len(data)  # what the codec will send
    if size > MAX_RESULT_BYTES:
        raise ApplicationError(
            f"{len(lines)} names are too many for one Temporal payload ({size} bytes compressed; limited to 2 MB)",
            type="ResultTooLarge",
            non_retryable=True,
        )
    return {name: data}


def in_scope(domain: str, names: Iterable[str]) -> set[str]:
    """Lowercased names under ``domain`` (or the domain itself) that look like hostnames."""
    out = set()
    for raw in names:
        name = raw.strip().lower().removeprefix("*.").rstrip(".")
        if (name == domain or name.endswith(f".{domain}")) and NAME_RE.match(name):
            out.add(name)
    return out


def read_lines(path: Path) -> list[str]:
    return path.read_text(errors="replace").splitlines() if path.exists() else []


def write_lines(path: Path, names: Iterable[str]) -> Path:
    path.write_text("".join(f"{n}\n" for n in sorted(names)))
    return path


# --- the tab ---------------------------------------------------------------------------


def begin(domain: str, step: str, text: str) -> None:
    """The first lines of a step in the tab: its run, its activity (and step), its attempt."""
    info = activity.info()
    what = info.activity_type if step == info.activity_type else f"{info.activity_type} [{step}]"
    print(f"\n[subdomains] >>> {info.workflow_id} {what} (attempt {info.attempt})", flush=True)
    say(domain, step, text)


def say(domain: str, step: str, text: str) -> None:
    print(f"[subdomains] [{domain}] {step}: {text}", flush=True)


# --- installed tools and data ----------------------------------------------------------


def tool(name: str) -> str:
    return os.environ.get(f"{name.upper()}_BIN", name)


def data_dir() -> Path:
    """Wordlists, resolvers and regulator, installed by entrypoint.sh."""
    return Path(os.environ.get("WARDEN_DATA_DIR", "/opt/warden/data/subdomains"))


def provider_config() -> Path:
    default = Path(__file__).resolve().parent / "config" / "provider-config.yaml"
    return Path(os.environ.get("SUBFINDER_PROVIDER_CONFIG", default))


def resolvers() -> list[str]:
    d = data_dir()
    return [
        "-r",
        str(d / "resolvers.txt"),
        "--resolvers-trusted",
        str(d / "trusted_resolvers.txt"),
        *PUREDNS_RATES,
    ]


# --- workflow side ---------------------------------------------------------------------


def is_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.CancelledError) or (
        isinstance(exc, (ActivityError, ChildWorkflowError)) and is_cancelled_exception(exc)
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
