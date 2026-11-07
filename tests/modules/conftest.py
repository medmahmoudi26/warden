"""Module tests: each module's workflows.py and activities.py on a Temporal dev server, with fake tools.

A module's files are imported as ``workflows``, ``activities`` and ``helpers``, the
names its worker uses on the fleet. Every module has files with those names, so
:func:`load` puts one module folder first on ``sys.path`` and re-imports them;
the Temporal sandbox re-imports ``workflows`` by name from that folder too.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest
from temporalio.worker import Worker

MODULES = Path(__file__).resolve().parents[2] / "modules"
NAMES = ("subdomains", "webs", "nuclei", "ffuf", "desync")
TOOLS = ("subfinder", "puredns", "gotator", "regulator", "httpx", "nuclei", "ffuf")
FAKE = Path(__file__).with_name("fake_tools.py")


@dataclass
class Module:
    workflows: ModuleType
    activities: ModuleType
    helpers: ModuleType


def load(name: str) -> Module:
    """Import module folder ``name``'s workflows.py, activities.py and helpers.py, replacing any other module's."""
    for folder in NAMES:
        while str(MODULES / folder) in sys.path:
            sys.path.remove(str(MODULES / folder))
    sys.path.insert(0, str(MODULES / name))
    for stale in ("workflows", "activities", "helpers"):
        sys.modules.pop(stale, None)
    return Module(*(importlib.import_module(m) for m in ("workflows", "activities", "helpers")))


@pytest.fixture
def tools(tmp_path: Path, monkeypatch) -> Path:
    """Fake tools on *_BIN, a data dir with tiny wordlists, and a state dir the fakes log to."""
    state = tmp_path / "state"
    state.mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    for t in TOOLS:
        w = bins / t
        w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {t} "$@"\n')
        w.chmod(0o755)
        monkeypatch.setenv(f"{t.upper()}_BIN", str(w))
    data = tmp_path / "data"
    data.mkdir()
    for f in (
        "resolvers.txt",
        "trusted_resolvers.txt",
        "subdomains.txt",
        "n0kovo_subdomains_huge.txt",
        "permutations_list.txt",
        "permutations_list_short.txt",
    ):
        (data / f).write_text("x\n")
    (data / "leaky-paths.txt").write_text("/.git/config\n/server-status\n/.env\n")
    (data / "templates-version").write_text("v10.5.1\n")
    monkeypatch.setenv("WARDEN_DATA_DIR", str(data))
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("THC_URL", "")
    monkeypatch.setenv("SUBFINDER_PROVIDER_CONFIG", str(tmp_path / "no-provider-config.yaml"))
    return state


def calls(state: Path, tool: str | None = None) -> list[list]:
    log = state / "calls.jsonl"
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [args for t, args in rows if tool is None or t == tool]


def worker(client, module: Module, workflows: list, activities: list, queue: str, monkeypatch) -> Worker:
    """A worker for a module's workflows and activities, heartbeating fast."""
    monkeypatch.setattr(module.helpers, "HEARTBEAT_EVERY_S", 0.2)
    return Worker(
        client,
        task_queue=queue,
        workflows=workflows,
        activities=activities,
        max_concurrent_activities=2,
        max_heartbeat_throttle_interval=timedelta(milliseconds=100),
        default_heartbeat_throttle_interval=timedelta(milliseconds=100),
    )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return bool(stat.strip()) and not stat.strip().startswith("Z")  # a zombie counts as gone


async def wait_for_pids(state: Path) -> list[int]:
    pids_file = state / "pids"
    for _ in range(200):
        if pids_file.exists() and pids_file.read_text().strip():
            return [int(p) for p in pids_file.read_text().split()]
        await asyncio.sleep(0.05)
    raise AssertionError("the slow tool never started")


async def all_gone(pids: list[int]) -> bool:
    for _ in range(200):
        if not any(alive(p) for p in pids):
            return True
        await asyncio.sleep(0.05)
    return False
