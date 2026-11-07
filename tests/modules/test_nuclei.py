"""modules/nuclei: NucleiScan and scan_chunk on a dev server with a fake nuclei; reconftw-style files; raw fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError

from tests.modules.conftest import all_gone, calls, load, wait_for_pids, worker
from warden.runs import collect_files

TQ = "test.nuclei"


@pytest.fixture
async def nuclei(dev_server, tools, monkeypatch):
    m = load("nuclei")
    async with worker(
        dev_server.client, m, [m.workflows.NucleiScan], [m.activities.scan_chunk], TQ, monkeypatch
    ):
        yield m


async def scan(client, nuclei, rid: str, targets: list[str]) -> dict:
    return await client.execute_workflow(
        nuclei.workflows.NucleiScan.run,
        nuclei.workflows.NucleiScanInput(targets=targets),
        id=rid,
        task_queue=TQ,
    )


async def test_findings_files_by_severity(nuclei, dev_server, tools: Path, tmp_path: Path) -> None:
    client, rid = dev_server.client, "run/test0001/nuclei-aaaaaa"
    res = await scan(client, nuclei, rid, ["https://a.example.com", "https://vuln.example.com"])
    assert res["findings"] == 3 and res["by_severity"]["high"] == 1 and res["by_severity"]["info"] == 2
    assert res["templates_version"] == "v10.5.1" and res["raw_dropped_chunks"] == 0
    (argv,) = calls(tools, "nuclei")
    assert argv[argv.index("-rl") + 1] == "150" and "-jsonl" in argv and "-duc" in argv
    assert not {"-t", "-s", "-severity", "-tags"} & set(argv)  # default templates, every severity

    files = await collect_files(client, rid, tmp_path)
    assert set(files) == {"nuclei.jsonl", "nuclei.txt", "nuclei-high.txt", "nuclei-info.txt"}
    high = Path(files["nuclei-high.txt"]["path"]).read_text().splitlines()
    assert high == ["[CVE-2021-41773] [http] [high] https://vuln.example.com/cgi-bin/.%2e/etc/passwd"]
    info = Path(files["nuclei-info.txt"]["path"]).read_text().splitlines()
    assert info[0] == '[tech-detect:nginx] [http] [info] https://a.example.com ["1.25"]'
    findings = [json.loads(x) for x in Path(files["nuclei.jsonl"]["path"]).read_text().splitlines()]
    assert any("request" in f for f in findings)


async def test_raw_requests_dropped_when_too_large(
    nuclei, dev_server, tools: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_RAW_BYTES", "2000000")  # about 6 KB once compressed
    monkeypatch.setattr(nuclei.helpers, "MAX_OUTPUT_BYTES", 1500)  # fits only without the raw fields
    rid = "run/test0001/nuclei-bbbbbb"
    res = await scan(dev_server.client, nuclei, rid, ["https://vuln.example.com"])
    assert res["raw_dropped_chunks"] == 1 and res["findings"] == 2
    files = await collect_files(dev_server.client, rid, tmp_path)
    findings = [json.loads(x) for x in Path(files["nuclei.jsonl"]["path"]).read_text().splitlines()]
    assert all("request" not in f and "response" not in f for f in findings)


async def test_cancellation_kills_nuclei(nuclei, dev_server, tools: Path) -> None:
    handle = await dev_server.client.start_workflow(
        nuclei.workflows.NucleiScan.run,
        nuclei.workflows.NucleiScanInput(targets=["https://slow.example.com"]),
        id="run/test0001/nuclei-cccccc",
        task_queue=TQ,
    )
    pids = await wait_for_pids(tools)
    await handle.cancel()
    with pytest.raises(WorkflowFailureError):
        await handle.result()
    assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED and await all_gone(pids)
