"""Runs: module workflows started on a fleet's task queue, observed through the Temporal client.

A run is a workflow with id ``run/<fleet_id>/<module>-<6 hex>`` on the queue
``<fleet_id>.<module>``, executed by that module's workers on the fleet. Warden
never imports module code: workflows are started by name.

Module activities may return files: ``{"warden_files": {"<name>": "<base64 of gzipped text>"}}``.
They are assembled from the run's history into ``<results_dir>/<fleet>-<module>-<id>/``
when the result is fetched, so output far larger than a Temporal payload reaches
the operator's disk, survives the fleet, and feeds later runs (``from_run``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import gzip
import json
import os
import re
import secrets
import zlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.service import RPCError

from warden.fleet.client import fleet_status, list_fleets
from warden.fleet.models import RUN_PREFIX, FleetState, FleetStatus
from warden.modules import Modules, task_queue, worker_id

RUN_ID_RE = re.compile(r"^run/[a-z0-9]{8}/([a-z][a-z0-9-]{0,23})-[0-9a-f]{6}$")
POLLERS_TIMEOUT_S = 900  # first installs build tools and fetch wordlists
POLLERS_INTERVAL_S = 2.0
SLOT_CHECK_EVERY_S = 5.0
RUN_TIMEOUT = timedelta(hours=24)  # further capped by the fleet's deadline
INLINE_RESULT_MAX = 20_000
PREVIEW_CHARS = 1_000
CANCEL_GRACE_S = 60
FILES_KEY = "warden_files"
FILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RESULT_FILE = "result.json"
HEAD_LINES = 10
HEAD_LINE_CHARS = 300


class RunError(RuntimeError):
    """A run could not be started or found; the message says why."""


def new_run_id(fleet_id: str, module: str) -> str:
    return f"{RUN_PREFIX}{fleet_id}/{module}-{secrets.token_hex(3)}"


def check_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id or ""):
        raise RunError(f"only run ids (run/<fleet_id>/<module>-<id>) are accepted, got {run_id!r}")
    return run_id


def run_module(run_id: str) -> str:
    m = RUN_ID_RE.match(check_run_id(run_id))
    assert m
    return m.group(1)


def run_dir(results_dir: Path, run_id: str) -> Path:
    """``<results_dir>/<fleet>-<module>-<id>``: where a run's files and large result go."""
    return Path(results_dir) / check_run_id(run_id).removeprefix(RUN_PREFIX).replace("/", "-")


# --- starting a run --------------------------------------------------------------


async def resolve_fleet(client: Client, fleet_id: str | None) -> FleetStatus:
    """The given fleet if READY, else the only READY fleet."""
    if fleet_id:
        st = await fleet_status(client, fleet_id)
        if st.state != FleetState.READY.value:
            raise RunError(f"fleet {fleet_id} is {st.state}, not READY")
        return st
    ready = [f for f in await list_fleets(client) if f.state == FleetState.READY.value]
    if len(ready) == 1:
        return ready[0]
    if not ready:
        raise RunError("no READY fleet; start one with fleet_up")
    raise RunError(f"several READY fleets ({', '.join(f.fleet_id for f in ready)}); pass fleet_id")


async def ensure_active(modules: Modules, fleet: FleetStatus, module: str) -> list[str]:
    """Worker ids of the module's running slots, activating one worker per machine if none run."""
    machines = [m.name for m in fleet.machines]

    def running() -> list[str]:
        return [
            worker_id(s.machine, module, s.slot)
            for s in modules.state(fleet.fleet_id, machines)
            if s.module == module and s.state == "running"
        ]

    ids = await asyncio.to_thread(running)
    if ids:
        return ids
    outcomes = await asyncio.to_thread(modules.activate, fleet.fleet_id, machines, module, 1)
    if all(o.error for o in outcomes):
        raise RunError(
            f"could not activate {module} on fleet {fleet.fleet_id}: "
            + "; ".join(f"{o.machine}: {o.error}" for o in outcomes)
        )
    ids = await asyncio.to_thread(running)
    if not ids:
        raise RunError(f"{module} has no running slot on fleet {fleet.fleet_id} after activation")
    return ids


async def pollers(client: Client, queue: str) -> set[str]:
    """Identities currently polling ``queue`` for workflow or activity tasks."""
    out: set[str] = set()
    for kind in (TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY):
        resp = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace, task_queue=TaskQueue(name=queue), task_queue_type=kind
            )
        )
        out |= {p.identity for p in resp.pollers}
    return out


async def wait_pollers(
    client: Client,
    queue: str,
    expected: list[str],
    timeout_s: float = POLLERS_TIMEOUT_S,
    interval_s: float | None = None,
    exited: Callable[[], Awaitable[list[str]]] | None = None,
) -> None:
    """Wait until every expected worker polls ``queue``. ``exited`` names expected slots whose
    entrypoint already exited (an install failed): those fail the wait at once."""
    interval_s = interval_s or POLLERS_INTERVAL_S
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    next_check = loop.time() + SLOT_CHECK_EVERY_S
    while True:
        missing = set(expected) - await pollers(client, queue)
        if not missing:
            return
        if exited is not None and loop.time() >= next_check:
            next_check = loop.time() + SLOT_CHECK_EVERY_S
            dead = await exited()
            if dead:
                raise RunError(
                    f"worker slots exited before polling {queue}: {', '.join(dead)} "
                    "(their tabs in Herdr show why; `hc read <machine>/<slot>`)"
                )
        if loop.time() >= deadline:
            raise RunError(
                f"workers not polling {queue} after {int(timeout_s)}s: {', '.join(sorted(missing))} "
                "(look at their tabs in Herdr, or `hc read <machine>/<slot>`)"
            )
        await asyncio.sleep(interval_s)


async def start_run(
    client: Client,
    modules: Modules,
    *,
    fleet_id: str | None,
    module: str,
    workflow: str,
    arg: Any,
    timeout: timedelta = RUN_TIMEOUT,
    pollers_timeout_s: float = POLLERS_TIMEOUT_S,
) -> dict[str, Any]:
    """Ensure the module runs on the fleet, wait for its workers to poll, then start ``workflow``."""
    fleet = await resolve_fleet(client, fleet_id)
    modules.get(module)
    expected = await ensure_active(modules, fleet, module)
    queue = task_queue(fleet.fleet_id, module)
    machines = [m.name for m in fleet.machines]

    async def exited() -> list[str]:
        slots = await asyncio.to_thread(modules.state, fleet.fleet_id, machines)
        return [
            f"{worker_id(s.machine, module, s.slot)} rc={s.rc}"
            for s in slots
            if s.module == module and s.state == "exited" and worker_id(s.machine, module, s.slot) in expected
        ]

    await wait_pollers(client, queue, expected, pollers_timeout_s, exited=exited)
    remaining = datetime.fromisoformat(fleet.deadline or "") - datetime.now(UTC)
    if remaining <= timedelta(0):
        raise RunError(f"fleet {fleet.fleet_id} is past its deadline")
    execution_timeout = min(timeout, remaining)
    run_id = new_run_id(fleet.fleet_id, module)
    await client.start_workflow(
        workflow, arg, id=run_id, task_queue=queue, execution_timeout=execution_timeout
    )
    return {
        "run_id": run_id,
        "fleet_id": fleet.fleet_id,
        "module": module,
        "workflow": workflow,
        "task_queue": queue,
        "workers": sorted(expected),
        "execution_timeout_s": int(execution_timeout.total_seconds()),
    }


# --- observing a run -------------------------------------------------------------


def _failure_message(exc: BaseException) -> str:
    messages = []
    cur: BaseException | None = exc
    while cur is not None:
        text = getattr(cur, "message", None) or str(cur)
        if text and text not in messages:
            messages.append(text)
        cur = cur.__cause__
    return ": ".join(messages[1:] or messages)


async def run_status(client: Client, run_id: str) -> dict[str, Any]:
    check_run_id(run_id)
    handle = client.get_workflow_handle(run_id)
    try:
        d = await handle.describe()
    except RPCError as exc:
        raise RunError(f"no run {run_id!r}") from exc
    out: dict[str, Any] = {
        "run_id": run_id,
        "state": d.status.name if d.status else "UNKNOWN",
        "workflow": d.workflow_type,
        "task_queue": d.task_queue,
        "started": d.start_time.isoformat() if d.start_time else None,
        "closed": d.close_time.isoformat() if d.close_time else None,
        "pending_activities": len(d.raw_description.pending_activities),
        "pending_children": len(d.raw_description.pending_children),
    }
    if d.status not in (WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.COMPLETED):
        try:
            await handle.result()
        except WorkflowFailureError as exc:
            out["failure"] = _failure_message(exc)
        except Exception as exc:  # noqa: BLE001
            out["failure"] = str(exc)
    return out


def _decode_file(name: Any, content: Any) -> bytes:
    if not isinstance(name, str) or not FILE_NAME_RE.match(name) or name == RESULT_FILE:
        raise RunError(f"invalid run file name {name!r}")
    if not isinstance(content, str):
        raise RunError(f"run file {name}: content is not a base64 string")
    try:
        return gzip.decompress(base64.b64decode(content, validate=True))
    except (binascii.Error, OSError, EOFError, zlib.error) as exc:
        raise RunError(f"run file {name}: undecodable content ({exc})") from exc


async def _gather_files(client: Client, handle: Any, parts: dict[str, list[bytes]]) -> None:
    async for event in handle.fetch_history_events():
        if event.HasField("child_workflow_execution_started_event_attributes"):
            child = event.child_workflow_execution_started_event_attributes.workflow_execution
            await _gather_files(
                client, client.get_workflow_handle(child.workflow_id, run_id=child.run_id), parts
            )
            continue
        if not event.HasField("activity_task_completed_event_attributes"):
            continue
        payloads = list(event.activity_task_completed_event_attributes.result.payloads)
        if not payloads:
            continue
        for value in await client.data_converter.decode(payloads):
            if isinstance(value, dict):
                files = value.get(FILES_KEY) or {}
                if not isinstance(files, dict):
                    raise RunError(f"malformed {FILES_KEY}: expected a name -> content mapping")
                for name, content in files.items():
                    parts.setdefault(name, []).append(_decode_file(name, content))


def _merge(name: str, parts: list[bytes]) -> list[str]:
    """Concatenate in completion order, drop blank and duplicate lines; sort ``.txt`` files."""
    seen: set[str] = set()
    lines: list[str] = []
    for part in parts:
        for line in part.decode("utf-8", errors="replace").splitlines():
            if line.strip() and line not in seen:
                seen.add(line)
                lines.append(line)
    if name.endswith(".txt"):
        lines.sort()
    return lines


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


async def collect_files(client: Client, run_id: str, results_dir: Path) -> dict[str, dict[str, Any]]:
    """Assemble the files returned by the run's completed activities into its run folder.

    Child workflows started by the run are followed. Only completed activities are
    in a history, so a retried step counts once, and a failed or cancelled run
    still yields what finished.
    """
    parts: dict[str, list[bytes]] = {}
    await _gather_files(client, client.get_workflow_handle(check_run_id(run_id)), parts)
    if not parts:
        return {}
    dest = run_dir(results_dir, run_id)
    dest.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(parts):
        lines = _merge(name, parts[name])
        path = dest / name
        _write_atomic(path, "".join(f"{line}\n" for line in lines))
        info: dict[str, Any] = {
            "path": str(path.resolve()),
            "lines": len(lines),
            "bytes": path.stat().st_size,
        }
        if name.endswith(".txt"):
            info["head"] = [line[:HEAD_LINE_CHARS] for line in lines[:HEAD_LINES]]
        out[name] = info
    return out


def deliver(
    run_id: str, value: Any, results_dir: Path, files: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The result inline (or in ``result.json`` when large), plus the run's files."""
    out: dict[str, Any] = {"run_id": run_id, "state": "COMPLETED"}
    text = json.dumps(value, default=str)
    if len(text) <= INLINE_RESULT_MAX:
        out["result"] = value
    else:
        dest = run_dir(results_dir, run_id)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / RESULT_FILE
        _write_atomic(path, text)
        out.update(result_file=str(path.resolve()), size=len(text), preview=text[:PREVIEW_CHARS])
    return _with_files(out, run_id, results_dir, files)


def _with_files(
    out: dict[str, Any], run_id: str, results_dir: Path, files: dict[str, dict[str, Any]] | None
) -> dict[str, Any]:
    if files:
        out["dir"] = str(run_dir(results_dir, run_id).resolve())
        out["files"] = files
    return out


async def run_result(client: Client, run_id: str, results_dir: Path) -> dict[str, Any]:
    st = await run_status(client, run_id)
    if st["state"] == "RUNNING":
        raise RunError(f"run {run_id} is still RUNNING")
    files = await collect_files(client, run_id, results_dir)
    if st["state"] != "COMPLETED":
        return _with_files(st, run_id, results_dir, files)
    value = await client.get_workflow_handle(run_id).result()
    return deliver(run_id, value, results_dir, files)


async def run_wait(client: Client, run_id: str, timeout_s: float, results_dir: Path) -> dict[str, Any]:
    timeout_s = max(1.0, min(float(timeout_s), 3600.0))
    await run_status(client, run_id)  # validates the id and that the run exists
    try:
        await asyncio.wait_for(client.get_workflow_handle(run_id).result(), timeout_s)
    except TimeoutError:
        return await run_status(client, run_id)
    except WorkflowFailureError:
        pass
    return await run_result(client, run_id, results_dir)


async def run_targets(
    client: Client, run_id: str, sources: dict[str, str], results_dir: Path, *, tool: str
) -> list[str]:
    """Lines of a completed run's output file, as the input of another run.

    ``sources`` maps the modules ``tool`` accepts as a source to the file it reads.
    """
    module = run_module(run_id)
    if module not in sources:
        accepted = " or ".join(sorted(sources))
        raise RunError(f"{tool} takes its targets from a {accepted} run, not a {module} run ({run_id})")
    st = await run_status(client, run_id)
    if st["state"] != "COMPLETED":
        raise RunError(f"run {run_id} is {st['state']}, not COMPLETED")
    path = run_dir(results_dir, run_id) / sources[module]
    if not path.is_file():
        await collect_files(client, run_id, results_dir)
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()] if path.is_file() else []
    if not lines:
        raise RunError(f"run {run_id} produced no {sources[module]}: nothing to take as targets")
    return lines


async def run_cancel(client: Client, run_id: str) -> dict[str, Any]:
    st = await run_status(client, run_id)
    if st["state"] == "RUNNING":
        await client.get_workflow_handle(run_id).cancel()
        st["cancel_requested"] = True
    return st


# --- module deactivation guard ---------------------------------------------------


async def runs_in_flight(client: Client, prefix: str) -> list[str]:
    """Running runs whose id starts with ``prefix``; their child workflows go with them."""
    return [
        wf.id
        async for wf in client.list_workflows("ExecutionStatus='Running'")
        if wf.id.startswith(prefix) and RUN_ID_RE.match(wf.id)
    ]


async def deactivate_module(
    client: Client, modules: Modules, fleet: FleetStatus, module: str, force: bool = False
) -> dict[str, Any]:
    """Stop a module's slots on a fleet, refusing while its runs are in flight unless forced."""
    modules.get(module)
    inflight = await runs_in_flight(client, f"{RUN_PREFIX}{fleet.fleet_id}/{module}-")
    if inflight and not force:
        raise RunError(
            f"{module} has runs in flight on fleet {fleet.fleet_id}: {', '.join(inflight)}; "
            "cancel them or pass force=true"
        )
    for rid in inflight:
        await client.get_workflow_handle(rid).cancel()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CANCEL_GRACE_S
    remaining = list(inflight)
    while remaining and loop.time() < deadline:
        await asyncio.sleep(1)
        remaining = [r for r in remaining if (await run_status(client, r))["state"] == "RUNNING"]
    for rid in remaining:  # their workers are about to stop, so they could never finish cancelling
        await client.get_workflow_handle(rid).terminate(reason=f"{module} deactivated")
    stopped = await asyncio.to_thread(
        modules.deactivate, fleet.fleet_id, [m.name for m in fleet.machines], module
    )
    return {"fleet_id": fleet.fleet_id, "module": module, "cancelled_runs": inflight, "stopped": stopped}
