"""The MCP surface: tool list, input normalisation, and fleet_status / module tools over a fake READY fleet."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
from fastmcp import Client as McpClient
from temporalio.worker import Worker

import warden.runs as runs
from tests.fakes import FakeHc, FakeHerdr
from tests.test_fleet_workflow import ACTIVITIES, reset
from warden.config import load_settings
from warden.fleet import client as fleets
from warden.fleet.models import FleetSpec
from warden.fleet.workflow import FleetWorkflow
from warden.mcp import (
    MAX_DOMAINS,
    MAX_TARGETS,
    TOOL_NAMES,
    App,
    build_mcp,
    normalize_domains,
    normalize_targets,
)
from warden.modules import Modules

ORCH = "test-mcp-orch"


def modules_dir(tmp_path: Path) -> Path:
    path = tmp_path / "modules" / "subdomains"
    path.mkdir(parents=True)
    entry = path / "entrypoint.sh"
    entry.write_text("#!/usr/bin/env bash\n# Passive subdomain enumeration.\nexec uv run worker.py\n")
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return path.parent


def app(tmp_path: Path, client=None, hc: FakeHc | None = None, herdr: FakeHerdr | None = None) -> App:
    settings = load_settings(
        {
            "WARDEN_MODULES_DIR": str(modules_dir(tmp_path)),
            "WARDEN_CONTROL_HOST": "mac.tail.ts.net",
            "WARDEN_ORCHESTRATOR_QUEUE": ORCH,
            "WARDEN_RESULTS_DIR": str(tmp_path / "results"),
        }
    )
    hc, herdr = hc or FakeHc(), herdr or FakeHerdr()
    return App(settings=settings, client=client, herdr=herdr, modules=Modules(settings, hc, herdr))


async def call(mcp, name: str, args: dict | None = None):
    async with McpClient(mcp) as c:
        return await c.call_tool(name, args or {}, raise_on_error=False)


async def test_tool_list_is_exactly_the_hand_written_set(tmp_path: Path) -> None:
    async with McpClient(build_mcp(app(tmp_path))) as c:
        names = [t.name for t in await c.list_tools()]
    assert sorted(names) == sorted(TOOL_NAMES) and len(TOOL_NAMES) == 16
    assert {
        "temporal_web_probe",
        "temporal_nuclei_scan",
        "temporal_web_fuzzing",
        "temporal_desync_scan",
    } <= set(names)
    assert all(n.split("_", 1)[0] in ("fleet", "module", "temporal") for n in names)


def test_normalize_domains() -> None:
    assert normalize_domains([" Example.COM ", "example.com", "", "# note"]) == ["example.com"]
    assert normalize_domains(["a.example.com\nb.example.org.\n\n# c"]) == ["a.example.com", "b.example.org"]
    with pytest.raises(ValueError, match="https://example.com/path"):
        normalize_domains(["example.com", "https://example.com/path"])
    with pytest.raises(ValueError, match="no domains"):
        normalize_domains(["", "# only a comment"])
    with pytest.raises(ValueError, match=f"at most {MAX_DOMAINS}"):
        normalize_domains([f"d{i}.example.com" for i in range(MAX_DOMAINS + 1)])


def test_normalize_targets() -> None:
    mixed = ["Example.com", "https://Example.com:8443/app", "example.com", "# x", " 10.0.0.1:8080 "]
    assert normalize_targets(mixed) == ["example.com", "https://example.com:8443/app", "10.0.0.1:8080"]
    assert normalize_targets(["HTTP://A.example.com/Path?q=1"]) == ["http://a.example.com/Path?q=1"]
    for bad in ("ftp://example.com", "example.com:99999", "https://user:pw@example.com", "not a host", "::1"):
        with pytest.raises(ValueError, match="not web targets"):
            normalize_targets(["example.com", bad])
    with pytest.raises(ValueError, match="probe hosts with temporal_web_probe first"):
        normalize_targets(["example.com"], urls_only=True)
    assert normalize_targets(["https://example.com/"], urls_only=True) == ["https://example.com/"]
    with pytest.raises(ValueError, match="no targets"):
        normalize_targets(["# nothing"])
    with pytest.raises(ValueError, match=f"at most {MAX_TARGETS} targets"):
        normalize_targets([f"h{i}.example.com" for i in range(MAX_TARGETS + 1)])
    with pytest.raises(ValueError, match="targets too large"):
        normalize_targets([f"https://{'a' * 50}.example.com/{i}" for i in range(30_000)])


@pytest.fixture
def started(monkeypatch) -> list[dict]:
    """Capture start_run calls instead of starting runs."""
    calls: list[dict] = []

    async def fake_start_run(client, modules, **kw):
        calls.append(kw)
        return {"run_id": f"run/abcd1234/{kw['module']}-aaaaaa", "fleet_id": "abcd1234"}

    monkeypatch.setattr(runs, "start_run", fake_start_run)
    return calls


async def test_subdomain_enumeration_options(tmp_path: Path, started) -> None:
    mcp = build_mcp(app(tmp_path))
    await call(mcp, "temporal_subdomain_enumeration", {"domains": ["example.com"]})
    await call(
        mcp,
        "temporal_subdomain_enumeration",
        {
            "domains": ["example.com"],
            "bruteforce": False,
            "permutations": False,
            "deep": True,
            "passive_minutes": 15,
        },
    )
    assert [c["arg"] for c in started] == [
        {
            "domains": ["example.com"],
            "bruteforce": True,
            "permutations": True,
            "deep": False,
            "passive_minutes": 180,
        },
        {
            "domains": ["example.com"],
            "bruteforce": False,
            "permutations": False,
            "deep": True,
            "passive_minutes": 15,
        },
    ]
    assert {c["module"] for c in started} == {"subdomains"} and started[0]["workflow"] == "SubdomainEnum"


async def test_web_tools_build_their_runs(tmp_path: Path, started, monkeypatch) -> None:
    sources: list[tuple] = []

    async def fake_targets(client, run_id, srcs, results_dir, *, tool):
        sources.append((run_id, srcs, tool))
        return ["https://a.example.com", "https://b.example.com:8443"]

    monkeypatch.setattr(runs, "run_targets", fake_targets)
    mcp = build_mcp(app(tmp_path))
    r = await call(mcp, "temporal_web_probe", {"targets": ["a.example.com"], "ports": [8443, 443, 443]})
    assert r.structured_content["targets"] == 1
    await call(mcp, "temporal_web_probe", {"from_run": "run/abcd1234/subdomains-bbbbbb"})
    await call(mcp, "temporal_nuclei_scan", {"from_run": "run/abcd1234/webs-cccccc"})
    await call(mcp, "temporal_web_fuzzing", {"targets": ["https://x.example.com/"], "chunk_size": 5})
    assert [(c["module"], c["workflow"], c["arg"]) for c in started] == [
        ("webs", "WebProbe", {"targets": ["a.example.com"], "chunk_size": 200, "ports": [443, 8443]}),
        (
            "webs",
            "WebProbe",
            {
                "targets": ["https://a.example.com", "https://b.example.com:8443"],
                "chunk_size": 200,
                "ports": [],
            },
        ),
        (
            "nuclei",
            "NucleiScan",
            {"targets": ["https://a.example.com", "https://b.example.com:8443"], "chunk_size": 25},
        ),
        ("ffuf", "WebFuzz", {"targets": ["https://x.example.com/"], "chunk_size": 5}),
    ]
    assert sources == [
        ("run/abcd1234/subdomains-bbbbbb", {"subdomains": "subdomains.txt"}, "temporal_web_probe"),
        ("run/abcd1234/webs-cccccc", {"webs": "urls.txt"}, "temporal_nuclei_scan"),
    ]


@pytest.mark.parametrize(
    ("tool", "args", "error"),
    [
        ("temporal_web_probe", {}, "either targets or from_run"),
        (
            "temporal_web_probe",
            {"targets": ["a.com"], "from_run": "run/abcd1234/subdomains-aaaaaa"},
            "either",
        ),
        ("temporal_web_probe", {"targets": ["a.com"], "ports": [0]}, "ports must be"),
        (
            "temporal_web_probe",
            {"targets": ["a.com"], "chunk_size": 0},
            "chunk_size must be between 1 and 1000",
        ),
        ("temporal_web_fuzzing", {"targets": ["example.com"]}, "probe hosts with temporal_web_probe first"),
        ("temporal_nuclei_scan", {"targets": ["ftp://x.com"]}, "not web targets: ftp://x.com"),
        ("temporal_desync_scan", {"targets": ["example.com"]}, "probe hosts with temporal_web_probe first"),
        (
            "temporal_desync_scan",
            {"targets": ["https://x.com/"], "checks": ["te-cl"]},
            "checks must be a non-empty subset of smuggle, split",
        ),
        ("temporal_desync_scan", {"targets": ["https://x.com/"], "checks": []}, "non-empty subset"),
        ("temporal_desync_scan", {"targets": ["https://x.com/"], "tier": 4}, "tier must be 1, 2 or 3"),
        (
            "temporal_desync_scan",
            {"targets": ["https://x.com/"], "rate_ms": 100},
            "rate_ms must be between 250",
        ),
        ("fleet_up", {"size": "s-2vcpu-4gb; rm"}, "invalid droplet size"),
        ("temporal_subdomain_enumeration", {"domains": ["a.com"], "passive_minutes": 0}, "passive_minutes"),
    ],
)
async def test_tool_input_errors(tmp_path: Path, started, tool: str, args: dict, error: str) -> None:
    res = await call(build_mcp(app(tmp_path)), tool, args)
    assert res.is_error and error in res.content[0].text and started == []


async def test_desync_scan_builds_its_run(tmp_path: Path, started, monkeypatch) -> None:
    sources: list[tuple] = []

    async def fake_targets(client, run_id, srcs, results_dir, *, tool):
        sources.append((run_id, srcs, tool))
        return ["https://a.example.com/login"]

    monkeypatch.setattr(runs, "run_targets", fake_targets)
    mcp = build_mcp(app(tmp_path))
    r = await call(
        mcp, "temporal_desync_scan", {"targets": ["HTTPS://B.example.com/x", "https://b.example.com/x"]}
    )
    assert r.structured_content["targets"] == 1
    await call(
        mcp,
        "temporal_desync_scan",
        {"from_run": "run/abcd1234/webs-dddddd", "checks": ["split", "smuggle", "split"], "sweep": True,
         "tier": 2, "rate_ms": 250, "chunk_size": 5},
    )  # fmt: skip
    await call(
        mcp, "temporal_desync_scan", {"targets": ["http://c.example.com:8080/"], "checks": ["SMUGGLE"]}
    )
    assert [(c["module"], c["workflow"]) for c in started] == [("desync", "DesyncScan")] * 3
    assert [c["arg"] for c in started] == [
        {"targets": ["https://b.example.com/x"], "checks": ["smuggle", "split"], "sweep": False, "tier": 1,
         "rate_ms": 1000, "chunk_size": 20},
        {"targets": ["https://a.example.com/login"], "checks": ["smuggle", "split"], "sweep": True, "tier": 2,
         "rate_ms": 250, "chunk_size": 5},
        {"targets": ["http://c.example.com:8080/"], "checks": ["smuggle"], "sweep": False, "tier": 1,
         "rate_ms": 1000, "chunk_size": 20},
    ]  # fmt: skip
    assert sources == [("run/abcd1234/webs-dddddd", {"webs": "urls.txt"}, "temporal_desync_scan")]


async def test_invalid_domains_fail_before_any_run(tmp_path: Path) -> None:
    mcp = build_mcp(app(tmp_path, client=None))  # no Temporal client: any start attempt would crash
    res = await call(mcp, "temporal_subdomain_enumeration", {"domains": ["https://example.com/path"]})
    assert res.is_error and "not hostnames: https://example.com/path" in res.content[0].text


async def test_temporal_tools_refuse_non_run_ids(tmp_path: Path) -> None:
    mcp = build_mcp(app(tmp_path))
    res = await call(mcp, "temporal_cancel", {"run_id": "fleet/abcd1234"})
    assert res.is_error and "only run ids" in res.content[0].text


async def test_module_list(tmp_path: Path) -> None:
    res = await call(build_mcp(app(tmp_path)), "module_list")
    assert res.structured_content["result"] == [
        {"name": "subdomains", "description": "Passive subdomain enumeration."}
    ]


async def test_fleet_up_rejects_unknown_modules(tmp_path: Path) -> None:
    res = await call(build_mcp(app(tmp_path)), "fleet_up", {"modules": ["nope"]})
    assert res.is_error and "available: subdomains" in res.content[0].text


async def test_fleet_status_and_module_tools_on_a_ready_fleet(tmp_path: Path, dev_server) -> None:
    reset()
    client = dev_server.client
    hc, herdr = FakeHc(), FakeHerdr()
    mcp = build_mcp(app(tmp_path, client=client, hc=hc, herdr=herdr))
    async with Worker(client, task_queue=ORCH, workflows=[FleetWorkflow], activities=ACTIVITIES):
        res = await call(mcp, "fleet_up", {"instances": 2, "size": "s-2vcpu-4gb"})
        fid = res.structured_content["fleet_id"]
        assert res.structured_content["state"] == "PROVISIONING"
        assert res.structured_content["size"] == "s-2vcpu-4gb"
        for _ in range(100):
            if (await fleets.fleet_status(client, fid)).state == "READY":
                break
            await asyncio.sleep(0.1)

        res = await call(mcp, "module_activate", {"fleet_id": fid, "module": "subdomains"})
        assert [m["started"] for m in res.structured_content["machines"]] == [[1], [1]]

        st = (await call(mcp, "fleet_status", {"fleet_id": fid})).structured_content
        m1 = st["machines"][0]
        space = herdr.space_ids[m1["name"]]
        assert st["state"] == "READY" and m1["herdr_space"] == space
        assert m1["modules"] == {"subdomains": [{"slot": 1, "state": "running", "rc": None}]}
        assert m1["hc_state"] == "connected"

        hc.disconnected.add(m1["name"])  # hc cannot reach it for a moment: slots unknown, not absent
        lost = (await call(mcp, "fleet_status", {"fleet_id": fid})).structured_content["machines"][0]
        assert lost["hc_state"] == "disconnected" and lost["modules"] is None
        hc.disconnected.clear()
        assert m1["raw_command"] == f"hc run --on {m1['name']} --bg --label herdr.space={space} -- <command>"

        listed = (await call(mcp, "fleet_list")).structured_content["result"]
        assert [(f["fleet_id"], f["machines"]) for f in listed] == [(fid, 2)]

        res = await call(mcp, "module_deactivate", {"fleet_id": fid, "module": "subdomains"})
        assert res.structured_content["stopped"] == {m["name"]: 1 for m in st["machines"]}

        res = await call(mcp, "fleet_down", {"fleet_id": fid})
        assert res.structured_content["destroy_requested"] is True
        await client.get_workflow_handle(f"fleet/{fid}").result()


async def test_module_activate_needs_a_ready_fleet(tmp_path: Path, dev_server) -> None:
    reset()
    client = dev_server.client
    mcp = build_mcp(app(tmp_path, client=client))
    async with Worker(client, task_queue=ORCH, workflows=[FleetWorkflow], activities=ACTIVITIES):
        await fleets.start_fleet(client, FleetSpec(fleet_id="gggg0001", instances=1), ORCH)
        await fleets.destroy_fleet(client, "gggg0001")
        await client.get_workflow_handle("fleet/gggg0001").result()
        res = await call(mcp, "module_activate", {"fleet_id": "gggg0001", "module": "subdomains"})
        assert res.is_error and "is DONE, not READY" in res.content[0].text
