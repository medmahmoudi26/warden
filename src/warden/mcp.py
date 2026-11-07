"""Warden's MCP tools, written by hand. Prefixes say what a tool wraps:

* ``fleet_``    fleet lifecycle (FleetWorkflow on the control plane);
* ``module_``   module folders run on a fleet (hc + Herdr.app);
* ``temporal_`` the Temporal client, on a module's behalf.

Adding a capability is a module folder plus one ``temporal_`` tool here.
Anything else is done with ``hc`` directly (see ``fleet_status``'s raw commands).
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import json
import re
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, TypeVar

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from temporalio.client import Client

from warden import runs
from warden.config import ConfigError, Settings
from warden.fleet import client as fleets
from warden.fleet.models import FleetSpec, FleetState, FleetStatus, new_fleet_id
from warden.modules import ModuleError, Modules, UnknownModuleError

TOOL_NAMES = (
    "fleet_up",
    "fleet_list",
    "fleet_status",
    "fleet_down",
    "module_list",
    "module_activate",
    "module_deactivate",
    "temporal_subdomain_enumeration",
    "temporal_web_probe",
    "temporal_nuclei_scan",
    "temporal_web_fuzzing",
    "temporal_desync_scan",
    "temporal_status",
    "temporal_wait",
    "temporal_result",
    "temporal_cancel",
)

INSTRUCTIONS = """Warden runs fleets of DigitalOcean machines, reachable over Tailscale and visible in Herdr.app.
fleet_* start, list, inspect and destroy fleets. module_* start and stop module folders on a fleet; each
module worker is a tab in the machine's "warden <machine>" space. temporal_* run module workflows and
follow them. For anything else, use hc directly on a fleet machine; fleet_status gives the command."""

MAX_DOMAINS = 5_000  # one child workflow per domain; keeps the run's history far below its event limit
MAX_TARGETS = 50_000
MAX_INPUT_BYTES = 1_500_000  # Temporal rejects payloads over 2 MB
MAX_PORTS = 100
DESYNC_CHECKS = ("smuggle", "split")
DESYNC_RATE_MS = (250, 60_000)
SIZE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$"
)

# from_run sources: the modules each tool takes targets from, and the file it reads.
WEB_PROBE_SOURCES = {"subdomains": "subdomains.txt"}
URL_SOURCES = {"webs": "urls.txt"}

USER_ERRORS = (
    runs.RunError,
    UnknownModuleError,
    ModuleError,
    fleets.FleetNotFound,
    ValueError,
    ConfigError,
)


@dataclass
class App:
    settings: Settings
    client: Client
    herdr: Any
    modules: Modules


F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def _errors_as_tool_errors(fn: F) -> F:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


def normalize_domains(domains: list[str]) -> list[str]:
    """Trim, lowercase, drop blanks and ``#`` lines, deduplicate; reject anything that is not a hostname."""
    out: dict[str, None] = {}
    invalid: list[str] = []
    for raw in domains:
        for line in str(raw).splitlines() or [""]:
            value = line.strip().lower().rstrip(".")
            if not value or value.startswith("#"):
                continue
            if HOSTNAME_RE.match(value):
                out[value] = None
            elif value not in invalid:
                invalid.append(value)
    if invalid:
        shown = ", ".join(invalid[:10]) + (f" (+{len(invalid) - 10} more)" if len(invalid) > 10 else "")
        raise ValueError(f"not hostnames: {shown}")
    if not out:
        raise ValueError("no domains given")
    if len(out) > MAX_DOMAINS:
        raise ValueError(f"at most {MAX_DOMAINS} domains per run, got {len(out)}")
    return list(out)


def _host_ok(host: str) -> bool:
    if HOSTNAME_RE.match(host):
        return True
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
    except ValueError:
        return False


def _target(value: str, *, urls_only: bool) -> str | None:
    """Normalised form of one web target, or None when it is not one."""
    if "://" in value:
        try:
            parts = urllib.parse.urlsplit(value)
            port = parts.port
        except ValueError:
            return None
        scheme, host = parts.scheme.lower(), (parts.hostname or "")
        if scheme not in ("http", "https") or not _host_ok(host) or parts.username or parts.password:
            return None
        netloc = f"{host}:{port}" if port else host
        return urllib.parse.urlunsplit((scheme, netloc, parts.path, parts.query, ""))
    if urls_only:
        return None
    host, _, port = value.lower().partition(":")
    if port and not (port.isdigit() and 1 <= int(port) <= 65535):
        return None
    return value.lower() if _host_ok(host) else None


def normalize_targets(targets: list[str], *, urls_only: bool = False) -> list[str]:
    """Web targets (hosts, IPv4, host:port, http(s) URLs; URLs only for fuzzing): trimmed,
    hostnames and schemes lowercased, blanks and ``#`` lines dropped, deduplicated, limited."""
    out: dict[str, None] = {}
    invalid: list[str] = []
    for raw in targets:
        for line in str(raw).splitlines() or [""]:
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            norm = _target(value, urls_only=urls_only)
            if norm:
                out[norm] = None
            elif value not in invalid:
                invalid.append(value)
    if invalid:
        shown = ", ".join(invalid[:10]) + (f" (+{len(invalid) - 10} more)" if len(invalid) > 10 else "")
        what = "http(s) URLs (probe hosts with temporal_web_probe first)" if urls_only else "web targets"
        raise ValueError(f"not {what}: {shown}")
    if not out:
        raise ValueError("no targets given")
    if len(out) > MAX_TARGETS:
        raise ValueError(
            f"at most {MAX_TARGETS} targets per run, got {len(out)}; split them into several runs"
        )
    size = len(json.dumps(list(out)))
    if size > MAX_INPUT_BYTES:
        raise ValueError(
            f"targets too large ({size} bytes, at most {MAX_INPUT_BYTES}); split them into several runs"
        )
    return list(out)


def check_chunk_size(chunk_size: int, maximum: int = 1000) -> int:
    if not 1 <= chunk_size <= maximum:
        raise ValueError(f"chunk_size must be between 1 and {maximum}")
    return chunk_size


def _space_ids(herdr: Any) -> dict[str, str]:
    """Machine -> machine-local id of its ``warden <machine>`` space (empty without a Herdr window)."""
    try:
        spaces = herdr.spaces()
    except Exception:  # noqa: BLE001 — the Herdr window is best effort
        return {}
    out = {}
    for s in spaces:
        machine = s.get("machine")
        if machine and s.get("name") == f"warden {machine}":
            out[machine] = str(s["id"]).removeprefix(f"{machine}/")
    return out


def build_mcp(app: App) -> FastMCP:
    mcp = FastMCP("warden", instructions=INSTRUCTIONS)
    tool = mcp.tool

    async def ready_fleet(fleet_id: str) -> FleetStatus:
        st = await fleets.fleet_status(app.client, fleet_id)
        if st.state != FleetState.READY.value:
            raise runs.RunError(f"fleet {fleet_id} is {st.state}, not READY")
        return st

    # --- fleets ----------------------------------------------------------------

    @tool
    @_errors_as_tool_errors
    async def fleet_up(
        instances: int = 1,
        region: str | None = None,
        size: str | None = None,
        max_lifetime_minutes: int | None = None,
        modules: list[str] | None = None,
    ) -> dict[str, Any]:
        """Start a fleet of DigitalOcean machines. Returns at once with state PROVISIONING; poll
        fleet_status until READY (about 3-5 minutes). `modules` are activated once it is READY.
        `size` is a droplet size slug (default from WARDEN_DO_SIZE; use s-2vcpu-4gb or larger for
        nuclei). The fleet destroys itself after max_lifetime_minutes (default 120)."""
        names = list(modules or [])
        for name in names:
            app.modules.get(name)
        if size is not None and not SIZE_RE.match(size):
            raise ValueError(f"invalid droplet size {size!r}")
        spec = FleetSpec(
            fleet_id=new_fleet_id(),
            instances=instances,
            region=region,
            size=size,
            max_lifetime_min=max_lifetime_minutes or app.settings.fleet_max_lifetime_min,
            ready_timeout_min=app.settings.ready_timeout_min,
            modules=names,
        )
        st = await fleets.start_fleet(app.client, spec, app.settings.orchestrator_queue)
        return {
            "fleet_id": st.fleet_id,
            "state": st.state,
            "instances": instances,
            "size": size or app.settings.do_size,
            "max_lifetime_minutes": spec.max_lifetime_min,
            "modules": names,
        }

    @tool
    @_errors_as_tool_errors
    async def fleet_list() -> list[dict[str, Any]]:
        """Fleets that are not yet DONE: id, state, deadline and machine count."""
        return [
            {"fleet_id": f.fleet_id, "state": f.state, "deadline": f.deadline, "machines": len(f.machines)}
            for f in await fleets.list_fleets(app.client)
        ]

    @tool
    @_errors_as_tool_errors
    async def fleet_status(fleet_id: str) -> dict[str, Any]:
        """A fleet's state, deadline and machines. Per machine: tailnet host, public IP, hc version,
        hc connection state, its Herdr space, module slots (running or exited; null while hc cannot
        reach the machine), and the hc command for a raw terminal."""
        st = await fleets.fleet_status(app.client, fleet_id)
        out = {k: v for k, v in asdict(st).items() if k != "machines"}
        names = [m.name for m in st.machines]
        ready = st.state == FleetState.READY.value
        spaces = _space_ids(app.herdr) if ready else {}
        slots, hc_states = (
            await asyncio.to_thread(app.modules.snapshot, st.fleet_id, names) if ready else ([], {})
        )
        machines = []
        for m in st.machines:
            space = spaces.get(m.name)
            label = f" --label herdr.space={space}" if space else ""
            mods: dict[str, list[dict[str, Any]]] = {}
            for s in slots:
                if s.machine == m.name:
                    mods.setdefault(s.module, []).append({"slot": s.slot, "state": s.state, "rc": s.rc})
            hc_state = hc_states.get(m.name) if ready else None
            machines.append(
                {
                    **asdict(m),
                    "hc_state": hc_state,
                    "herdr_space": space,
                    # unknown (None) while hc cannot reach the machine, rather than "no slots"
                    "modules": mods if hc_state in (None, "connected") else None,
                    "raw_command": f"hc run --on {m.name} --bg{label} -- <command>" if ready else None,
                }
            )
        out["machines"] = machines
        return out

    @tool
    @_errors_as_tool_errors
    async def fleet_down(fleet_id: str) -> dict[str, Any]:
        """Destroy a fleet: terminate its runs, close its Herdr spaces, delete its machines and tailnet
        devices. Returns at once; already-destroyed fleets are left as they are."""
        st = await fleets.destroy_fleet(app.client, fleet_id, "requested")
        return {"fleet_id": st.fleet_id, "state": st.state, "destroy_requested": True}

    # --- modules ---------------------------------------------------------------

    @tool
    @_errors_as_tool_errors
    async def module_list() -> list[dict[str, str]]:
        """Module folders that can be activated on a fleet."""
        return [{"name": m.name, "description": m.description} for m in app.modules.list()]

    @tool
    @_errors_as_tool_errors
    async def module_activate(fleet_id: str, module: str, workers: int = 1) -> dict[str, Any]:
        """Run a module on every machine of a READY fleet, with `workers` slots (tabs) per machine.
        Idempotent: running slots are left alone, missing or exited ones are started."""
        st = await ready_fleet(fleet_id)
        app.modules.get(module)
        outcomes = await asyncio.to_thread(
            app.modules.activate, st.fleet_id, [m.name for m in st.machines], module, workers
        )
        return {"fleet_id": st.fleet_id, "module": module, "machines": [asdict(o) for o in outcomes]}

    @tool
    @_errors_as_tool_errors
    async def module_deactivate(fleet_id: str, module: str, force: bool = False) -> dict[str, Any]:
        """Stop a module's slots on a fleet. Refused while its runs are in flight unless force=true,
        which cancels them first."""
        st = await fleets.fleet_status(app.client, fleet_id)
        return await runs.deactivate_module(app.client, app.modules, st, module, force)

    # --- temporal: module workflows ---------------------------------------------

    async def web_targets(
        targets: list[str] | None,
        from_run: str | None,
        sources: dict[str, str],
        *,
        tool: str,
        urls_only: bool,
    ) -> list[str]:
        if (targets is None) == (from_run is None):
            raise ValueError("give either targets or from_run")
        if from_run is not None:
            targets = await runs.run_targets(
                app.client, from_run, sources, app.settings.results_dir, tool=tool
            )
        return normalize_targets(targets or [], urls_only=urls_only)

    @tool
    @_errors_as_tool_errors
    async def temporal_subdomain_enumeration(
        domains: list[str],
        fleet_id: str | None = None,
        bruteforce: bool = True,
        permutations: bool = True,
        deep: bool = False,
        passive_minutes: int = 180,
    ) -> dict[str, Any]:
        """reconftw-style subdomain enumeration of root domains across a fleet. Per domain, a child
        workflow runs each step as its own activity: subfinder (all sources, the module's provider
        keys), DNS resolution with wildcard filtering, DNS brute force (bruteforce), and gotator +
        regulator permutations (permutations). Active: heavy DNS traffic, about 25 minutes per
        domain with brute force; deep uses bigger wordlists. passive_minutes bounds subfinder
        (reconftw's 180 by default; slow keyed sources such as VirusTotal can use all of it). Output:
        subdomains.txt (resolved) and passive.txt. Returns a run id at once; follow it with temporal_wait. fleet_id may be
        omitted when exactly one fleet is READY."""
        if not 1 <= passive_minutes <= 1440:
            raise ValueError("passive_minutes must be between 1 and 1440")
        targets = normalize_domains(domains)
        started = await runs.start_run(
            app.client,
            app.modules,
            fleet_id=fleet_id,
            module="subdomains",
            workflow="SubdomainEnum",
            arg={
                "domains": targets,
                "bruteforce": bruteforce,
                "permutations": permutations,
                "deep": deep,
                "passive_minutes": passive_minutes,
            },
        )
        return {**started, "domains": len(targets)}

    @tool
    @_errors_as_tool_errors
    async def temporal_web_probe(
        targets: list[str] | None = None,
        from_run: str | None = None,
        ports: list[int] | None = None,
        fleet_id: str | None = None,
        chunk_size: int = 200,
    ) -> dict[str, Any]:
        """Probe hosts or URLs with httpx across a fleet (module webs), recording status, length,
        content type, title, server, technologies, CNAMEs, IPs, CDN, redirect, favicon and body
        hashes, response time and TLS data. Give targets, or from_run: a completed
        temporal_subdomain_enumeration run (its subdomains.txt). ports overrides httpx's default
        80/443. Output: webs.jsonl, webs.txt (readable) and urls.txt (live URLs, the input of
        temporal_nuclei_scan and temporal_web_fuzzing). Returns a run id at once."""
        check_chunk_size(chunk_size)
        hosts = await web_targets(
            targets, from_run, WEB_PROBE_SOURCES, tool="temporal_web_probe", urls_only=False
        )
        wanted = sorted(set(ports or []))
        if len(wanted) > MAX_PORTS or any(not 1 <= p <= 65535 for p in wanted):
            raise ValueError(f"ports must be at most {MAX_PORTS} numbers between 1 and 65535")
        started = await runs.start_run(
            app.client,
            app.modules,
            fleet_id=fleet_id,
            module="webs",
            workflow="WebProbe",
            arg={"targets": hosts, "chunk_size": chunk_size, "ports": wanted},
        )
        return {**started, "targets": len(hosts)}

    @tool
    @_errors_as_tool_errors
    async def temporal_nuclei_scan(
        targets: list[str] | None = None,
        from_run: str | None = None,
        fleet_id: str | None = None,
        chunk_size: int = 25,
    ) -> dict[str, Any]:
        """Scan web targets with all of nuclei's default templates at every severity, across a
        fleet (module nuclei). Give targets (URLs, or hosts nuclei probes first), or from_run: a
        completed temporal_web_probe run (its urls.txt). Active and slow: about 150 requests per
        second per worker. Output: nuclei.jsonl, nuclei.txt and nuclei-<severity>.txt. Returns a
        run id at once."""
        check_chunk_size(chunk_size)
        urls = await web_targets(targets, from_run, URL_SOURCES, tool="temporal_nuclei_scan", urls_only=False)
        started = await runs.start_run(
            app.client,
            app.modules,
            fleet_id=fleet_id,
            module="nuclei",
            workflow="NucleiScan",
            arg={"targets": urls, "chunk_size": chunk_size},
        )
        return {**started, "targets": len(urls)}

    @tool
    @_errors_as_tool_errors
    async def temporal_web_fuzzing(
        targets: list[str] | None = None,
        from_run: str | None = None,
        fleet_id: str | None = None,
        chunk_size: int = 5,
    ) -> dict[str, Any]:
        """Fuzz web URLs for sensitive paths with ffuf and the leaky-paths wordlist, reconftw-style
        (all statuses but 404, auto-calibration, at most 15 minutes per URL), across a fleet (module
        ffuf). Give http(s) URLs, or from_run: a completed temporal_web_probe run (its urls.txt).
        Output: ffuf.jsonl and ffuf.txt (`<status> <length> <url>`). Returns a run id at once."""
        check_chunk_size(chunk_size)
        urls = await web_targets(targets, from_run, URL_SOURCES, tool="temporal_web_fuzzing", urls_only=True)
        started = await runs.start_run(
            app.client,
            app.modules,
            fleet_id=fleet_id,
            module="ffuf",
            workflow="WebFuzz",
            arg={"targets": urls, "chunk_size": chunk_size},
        )
        return {**started, "targets": len(urls)}

    @tool
    @_errors_as_tool_errors
    async def temporal_desync_scan(
        targets: list[str] | None = None,
        from_run: str | None = None,
        checks: list[str] | None = None,
        sweep: bool = False,
        tier: int = 1,
        rate_ms: int = 1000,
        fleet_id: str | None = None,
        chunk_size: int = 20,
    ) -> dict[str, Any]:
        """Probe web URLs for HTTP desync bugs on raw sockets, across a fleet (module desync). Give
        http(s) URLs, or from_run: a completed temporal_web_probe run (its urls.txt). checks:
        "smuggle" (CL.0 through an obfuscated Content-Length line: benign, attack, benign pipelined
        on one connection; the 5 lines from paid reports, or the whole ~3,600-member family with
        sweep) and/or "split" (CRLF injection through Unicode folds such as %DC%8A in the path,
        each against the same vector one codepoint higher; screened with 4 vectors, the tier's
        vectors where a URL reacts, all of them with sweep; tier 1-3). Each URL must answer its
        benign request the same way 3 times or it is skipped; a signal its control reproduces is
        withdrawn. Detection only, one connection per host per rate_ms, 10 minutes per URL (60 with
        sweep). Sends malformed requests: authorised scope only. Output: desync.jsonl (every
        probe), desync.txt (leads), skipped.txt (URLs not fully probed, and why). Returns a run id
        at once."""
        check_chunk_size(chunk_size)
        wanted = list(DESYNC_CHECKS) if checks is None else [c.strip().lower() for c in checks]
        if not wanted or set(wanted) - set(DESYNC_CHECKS):
            raise ValueError(f"checks must be a non-empty subset of {', '.join(DESYNC_CHECKS)}")
        if not 1 <= tier <= 3:
            raise ValueError("tier must be 1, 2 or 3")
        if not DESYNC_RATE_MS[0] <= rate_ms <= DESYNC_RATE_MS[1]:
            raise ValueError(f"rate_ms must be between {DESYNC_RATE_MS[0]} and {DESYNC_RATE_MS[1]}")
        urls = await web_targets(targets, from_run, URL_SOURCES, tool="temporal_desync_scan", urls_only=True)
        started = await runs.start_run(
            app.client,
            app.modules,
            fleet_id=fleet_id,
            module="desync",
            workflow="DesyncScan",
            arg={
                "targets": urls,
                "checks": [c for c in DESYNC_CHECKS if c in wanted],
                "sweep": sweep,
                "tier": tier,
                "rate_ms": rate_ms,
                "chunk_size": chunk_size,
            },
        )
        return {**started, "targets": len(urls)}

    # --- temporal: following runs ------------------------------------------------

    @tool
    @_errors_as_tool_errors
    async def temporal_status(run_id: str) -> dict[str, Any]:
        """A run's state, times, task queue, pending activities, and failure if any."""
        return await runs.run_status(app.client, run_id)

    @tool
    @_errors_as_tool_errors
    async def temporal_wait(run_id: str, timeout_s: int = 600) -> dict[str, Any]:
        """Wait up to timeout_s (max 3600) for a run to finish. Returns what temporal_result returns
        when it finishes in time (result and files), otherwise its status."""
        return await runs.run_wait(app.client, run_id, timeout_s, app.settings.results_dir)

    @tool
    @_errors_as_tool_errors
    async def temporal_result(run_id: str) -> dict[str, Any]:
        """A finished run's state, result and output files. Files are assembled under
        state/results/<run>/ and listed with path, line count, size and (for .txt) the first lines;
        failed or cancelled runs still list the files of their finished chunks. Large results go to
        result.json in that folder (path, size and preview returned)."""
        return await runs.run_result(app.client, run_id, app.settings.results_dir)

    @tool
    @_errors_as_tool_errors
    async def temporal_cancel(run_id: str) -> dict[str, Any]:
        """Cancel a running run (its tool processes are stopped). Finished runs are left as they are."""
        return await runs.run_cancel(app.client, run_id)

    return mcp
