"""modules/ffuf: WebFuzz and fuzz_chunk on a dev server with a fake ffuf, and reconftw-style files."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

from tests.modules.conftest import all_gone, calls, load, wait_for_pids, worker
from warden.runs import collect_files

TQ = "test.ffuf"


@pytest.fixture
async def ffuf(dev_server, tools, monkeypatch):
    m = load("ffuf")
    async with worker(
        dev_server.client, m, [m.workflows.WebFuzz], [m.activities.fuzz_chunk], TQ, monkeypatch
    ):
        yield m


async def fuzz(client, ffuf, rid: str, **kw) -> dict:
    return await client.execute_workflow(
        ffuf.workflows.WebFuzz.run, ffuf.workflows.WebFuzzInput(**kw), id=rid, task_queue=TQ
    )


def test_fuzz_base() -> None:
    fuzz_base = load("ffuf").helpers.fuzz_base
    assert fuzz_base("https://example.com/") == "https://example.com"
    assert fuzz_base("https://example.com:8443/app/?q=1#x") == "https://example.com:8443/app"


def test_relative_wordlist(tmp_path: Path) -> None:
    """Leading slashes go, so ffuf's calibration probes and the entries share <base>/FUZZ."""
    source = tmp_path / "leaky-paths.txt"
    source.write_text("/.git/config\n/?wsdl\n/\n\n.env\n/.env\n")
    out = load("ffuf").helpers.relative_wordlist(source, tmp_path / "relative.txt")
    assert out.read_text().splitlines() == [".git/config", "?wsdl", ".env"]


async def test_fuzzing_files_and_reconftw_flags(ffuf, dev_server, tools: Path, tmp_path: Path) -> None:
    client, rid = dev_server.client, "run/test0001/ffuf-aaaaaa"
    targets = ["https://b.example.com/", "https://a.example.com", "https://error-fail.example.com"]
    res = await fuzz(client, ffuf, rid, targets=targets)
    assert res["results"] == 4 and res["url_errors"] == {"https://error-fail.example.com": "ffuf exited 2"}
    first = calls(tools, "ffuf")[0]
    assert first[first.index("-u") + 1] == "https://b.example.com/FUZZ"
    for flag, value in (("-mc", "all"), ("-fc", "404"), ("-maxtime", "900")):
        assert first[first.index(flag) + 1] == value
    assert {"-ac", "-sf", "-noninteractive", "-json"} <= set(first)
    assert first[first.index("-w") + 1].endswith("leaky-paths.txt")

    files = await collect_files(client, rid, tmp_path)
    assert Path(files["ffuf.txt"]["path"]).read_text().splitlines() == [
        "200 92 https://a.example.com/.git/config",
        "200 92 https://b.example.com/.git/config",
        "200 93 https://a.example.com/server-status",
        "200 93 https://b.example.com/server-status",
    ]
    assert files["ffuf.jsonl"]["lines"] == 4


async def test_chunk_fails_when_every_url_fails(ffuf, dev_server, tools: Path) -> None:
    targets = ["https://ok.example.com", "https://fail.example.com"]
    res = await fuzz(dev_server.client, ffuf, "run/test0001/ffuf-bbbbbb", targets=targets, chunk_size=1)
    assert res["chunks"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert "ffuf failed on every URL" in res["failed_chunks"][0]["error"]


async def test_cancellation_kills_ffuf(ffuf, dev_server, tools: Path) -> None:
    handle = await dev_server.client.start_workflow(
        ffuf.workflows.WebFuzz.run,
        ffuf.workflows.WebFuzzInput(targets=["https://slow.example.com"]),
        id="run/test0001/ffuf-cccccc",
        task_queue=TQ,
    )
    pids = await wait_for_pids(tools)
    await handle.cancel()
    with pytest.raises(WorkflowFailureError):
        await handle.result()
    assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED and await all_gone(pids)
