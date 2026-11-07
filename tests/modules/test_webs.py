"""modules/webs: WebProbe and probe_chunk on a dev server with a fake httpx, and the files Warden assembles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

from tests.modules.conftest import all_gone, calls, load, wait_for_pids, worker
from warden.runs import collect_files

TQ = "test.webs"


@pytest.fixture
async def webs(dev_server, tools, monkeypatch):
    m = load("webs")
    async with worker(
        dev_server.client, m, [m.workflows.WebProbe], [m.activities.probe_chunk], TQ, monkeypatch
    ):
        yield m


async def probe(client, webs, rid: str, **kw) -> dict:
    return await client.execute_workflow(
        webs.workflows.WebProbe.run, webs.workflows.WebProbeInput(**kw), id=rid, task_queue=TQ
    )


async def test_probe_writes_jsonl_readable_and_urls(webs, dev_server, tools: Path, tmp_path: Path) -> None:
    client, rid = dev_server.client, "run/test0001/webs-aaaaaa"
    targets = ["a.example.com", "dead.example.com", "https://b.example.com:8443/app"]
    res = await probe(client, webs, rid, targets=targets, ports=[443, 8443])
    assert (res["targets"], res["alive"], res["chunks"]["succeeded"]) == (3, 2, 1)
    (argv,) = calls(tools, "httpx")
    for flag in ("-sc", "-cl", "-ct", "-title", "-server", "-td", "-cname", "-ip", "-cdn", "-tls-grab", "-j"):
        assert flag in argv
    assert argv[argv.index("-rl") + 1] == "150" and argv[argv.index("-p") + 1] == "443,8443"

    files = await collect_files(client, rid, tmp_path)
    urls = Path(files["urls.txt"]["path"]).read_text().splitlines()
    assert urls == ["https://a.example.com", "https://b.example.com:8443/app"]
    assert Path(files["webs.txt"]["path"]).read_text().splitlines()[0] == (
        "https://a.example.com [200] [1234] [text/html] [Home] [nginx] [Nginx,React] [cdn.a.example.com] "
        "[192.0.2.1] [cloudflare]"
    )
    records = [json.loads(line) for line in Path(files["webs.jsonl"]["path"]).read_text().splitlines()]
    assert [r["input"] for r in records] == ["a.example.com", "https://b.example.com:8443/app"]


async def test_failed_chunk_is_retried_then_reported(webs, dev_server, tools: Path) -> None:
    targets = [f"h{i}.example.com" for i in range(4)] + ["fail.example.com"]
    res = await probe(dev_server.client, webs, "run/test0001/webs-bbbbbb", targets=targets, chunk_size=2)
    assert res["chunks"] == {"total": 3, "succeeded": 2, "failed": 1} and res["alive"] == 4
    assert res["failed_chunks"] == [{"targets": ["fail.example.com"], "error": "httpx exited 2"}]
    assert len([a for a in calls(tools, "httpx") if "-p" not in a]) == 4  # 2 chunks + the failing one twice


async def test_oversized_chunk_is_not_retried(webs, dev_server, tools: Path, monkeypatch) -> None:
    monkeypatch.setattr(webs.helpers, "MAX_OUTPUT_BYTES", 100)
    with pytest.raises(WorkflowFailureError) as exc:
        await probe(
            dev_server.client,
            webs,
            "run/test0001/webs-dddddd",
            targets=[f"h{i}.example.com" for i in range(20)],
        )
    assert "smaller chunk_size" in str(exc.value.cause) and len(calls(tools, "httpx")) == 1


async def test_cancellation_kills_httpx(webs, dev_server, tools: Path) -> None:
    handle = await dev_server.client.start_workflow(
        webs.workflows.WebProbe.run,
        webs.workflows.WebProbeInput(targets=["slow.example.com"]),
        id="run/test0001/webs-cccccc",
        task_queue=TQ,
    )
    pids = await wait_for_pids(tools)
    await handle.cancel()
    with pytest.raises(WorkflowFailureError):
        await handle.result()
    assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED and await all_gone(pids)
