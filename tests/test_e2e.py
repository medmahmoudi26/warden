"""Live end-to-end on DigitalOcean through the running warden-serve MCP server (opt-in: WARDEN_E2E=1).

Costs money (two s-1vcpu-1gb droplets for about an hour) and sends active traffic to
example.com (DNS brute force, httpx, ffuf). Needs warden-serve on
http://127.0.0.1:8008/mcp (or WARDEN_MCP_URL), Herdr.app, Temporal published on
the tailnet, and .env credentials. Everything it creates is destroyed at the end.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("WARDEN_E2E") != "1", reason="set WARDEN_E2E=1 to run (costs money)"
)

URL = os.environ.get("WARDEN_MCP_URL", "http://127.0.0.1:8008/mcp")


async def call(client, name: str, args: dict | None = None) -> dict:
    res = await client.call_tool(name, args or {}, timeout=3700)
    return res.structured_content


async def poll(fn, check, timeout_s: float, every_s: float = 15.0):
    deadline = time.monotonic() + timeout_s
    while True:
        value = await fn()
        if check(value):
            return value
        assert time.monotonic() < deadline, f"timed out; last value: {value}"
        await asyncio.sleep(every_s)


def slots_running(st: dict, module: str) -> bool:
    return st["state"] == "READY" and all(
        any(s["state"] == "running" for s in m["modules"].get(module, [])) for m in st["machines"]
    )


async def test_fleet_subdomains_end_to_end() -> None:
    from dotenv import load_dotenv
    from fastmcp import Client

    load_dotenv(".env")
    async with Client(URL) as c:
        up = await call(
            c,
            "fleet_up",
            {"instances": 2, "modules": ["subdomains", "webs", "ffuf"], "max_lifetime_minutes": 120},
        )
        fid = up["fleet_id"]
        try:
            st = await poll(
                lambda: call(c, "fleet_status", {"fleet_id": fid}),
                lambda s: (
                    s["state"] in ("FAILED", "DONE")
                    or all(slots_running(s, m) for m in ("subdomains", "webs", "ffuf"))
                ),
                timeout_s=30 * 60,
            )
            assert all(slots_running(st, m) for m in ("subdomains", "webs", "ffuf")), st
            assert all(m["herdr_space"] for m in st["machines"]), "every machine has a Warden space"

            run = await call(
                c,
                "temporal_subdomain_enumeration",
                {"domains": ["example.com"], "fleet_id": fid, "passive_minutes": 5},
            )
            res = await call(c, "temporal_wait", {"run_id": run["run_id"], "timeout_s": 3600})
            assert res["state"] == "COMPLETED", res
            found = Path(res["files"]["subdomains.txt"]["path"]).read_text().split()
            assert found and all(s == "example.com" or s.endswith(".example.com") for s in found)

            probe = await call(c, "temporal_web_probe", {"from_run": run["run_id"], "fleet_id": fid})
            webs = await call(c, "temporal_wait", {"run_id": probe["run_id"], "timeout_s": 1800})
            assert webs["state"] == "COMPLETED" and webs["result"]["alive"] > 0, webs

            fuzz = await call(c, "temporal_web_fuzzing", {"from_run": probe["run_id"], "fleet_id": fid})
            ffuf = await call(c, "temporal_wait", {"run_id": fuzz["run_id"], "timeout_s": 1800})
            assert ffuf["state"] == "COMPLETED", ffuf
        finally:
            await call(c, "fleet_down", {"fleet_id": fid})
            await poll(
                lambda: call(c, "fleet_status", {"fleet_id": fid}),
                lambda s: s["state"] in ("DONE", "FAILED"),
                timeout_s=20 * 60,
            )

    from warden.config import load_settings
    from warden.fleet.provider import do_api
    from warden.hc import HcDriver, HerdrApp
    from warden.tailscale import TailscaleAPI

    settings = load_settings()
    assert do_api(settings, "GET", f"/v2/droplets?tag_name=fleet:{fid}").get("droplets", []) == []
    assert TailscaleAPI(settings).devices(tag=settings.ts_tag, hostname_prefix=f"wf-{fid}-") == []
    assert not [m for m in HcDriver(settings.hc_bin).machines() if m.get("name", "").startswith(f"wf-{fid}-")]
    try:
        spaces = HerdrApp(settings.herdr_app_bin).spaces()
    except Exception:  # noqa: BLE001 — no Herdr window: nothing to check
        spaces = []
    assert not [s for s in spaces if str(s.get("machine") or "").startswith(f"wf-{fid}-")]
