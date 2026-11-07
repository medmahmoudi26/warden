"""Shared fixtures."""

from __future__ import annotations

import shutil

import pytest
from temporalio.testing import WorkflowEnvironment


@pytest.fixture
async def dev_server():
    """A throwaway Temporal dev server (real visibility and task-queue pollers), from the local
    `temporal` CLI on random ports. Skipped when the CLI is not installed."""
    path = shutil.which("temporal")
    if not path:
        pytest.skip("temporal CLI not installed")
    async with await WorkflowEnvironment.start_local(dev_server_existing_path=path, ui=False) as env:
        yield env
