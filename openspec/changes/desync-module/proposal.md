# Proposal

## Why

The fleet can find live web services (`webs`) and scan them with off-the-shelf tools (`nuclei`, `ffuf`), but it has nothing for HTTP desync bugs. These bugs come from two parsers disagreeing about where a request body ends, or about which bytes count as a newline. They paid well in past programs.

An earlier Go scanner finds them. But it is 10,000 lines built on a job runtime: crawled-exchange inputs, datasets, Nexus and a corpus store. We want its method on Warden as a plain module that runs from a `temporal_` tool, without any of that machinery.

## What Changes

- **New `desync` module.** A Temporal worker in pure Python that writes exact bytes on raw TCP/TLS sockets. It installs nothing beyond `uv`. It keeps the scanner's rules, not its code:
  - **Exact bytes.** Requests are written to the socket as given; no HTTP library touches them.
  - **The connection is the unit of observation.** The smuggling check pipelines a benign request, then an attack, then the same benign request on one socket. It asks whether the third answer changed.
  - **Stability gate.** Each target's benign request is sent several times first. A target that does not answer it the same way each time is refused, not scanned.
  - **Matched controls only remove signals.** Each smuggling attack that fires is re-sent with a well-formed `Content-Length`. A fold vector is always compared against the same vector one codepoint higher. If the control also fires, the signal is withdrawn.
  - **Detection only.** The smuggled body is an incomplete request. Canaries use `.invalid` names. Nothing is sent that could complete another user's request.
  - **Polite by default.** One connection per second per host, configurable.
  - **Gaps are reported.** A target that is refused, unreachable or out of time budget is listed with the reason, never silently dropped.
- **Two checks:**
  - `smuggle`: CL.0 through an obfuscated `Content-Length` header line. By default it sends the five header lines from paid reports (`\tContent-Length`, `Content-Length\t:\t`, an obs-fold, `Content-Length: +`, and the lowercase `+` form). With `sweep`, it sends the whole family: every position × hostile byte × casing, under a per-target time budget.
  - `split`: CRLF injection through lossy Unicode narrowing in the URL path. A codepoint whose low byte is `0x0A` or `0x0D`, such as `%DC%8A` (U+070A), carries an injected `Host:` canary. The check first screens each target with two vectors per class. Only targets that react get the full vector set for the chosen tier.
- **New MCP tool `temporal_desync_scan`.** It takes `http(s)` URLs, or `from_run` pointing to a completed `webs` run (its `urls.txt`). It also accepts `checks`, `sweep`, `tier`, `rate_ms` and `chunk_size`, and starts the `DesyncScan` workflow.
- **Output files:**
  - `desync.jsonl`: one observation per probe;
  - `desync.txt`: one readable line per lead that survived its control;
  - `skipped.txt`: targets not fully scanned, and why.

  The result is counts only.
- **Left out from the earlier scanner:**
  - crawled request/response units and injection-point discovery (headers, cookies, JSON bodies). Only the URL path is injected;
  - HTTP/2;
  - the second-source "reach" severity oracle;
  - corpus publishing;
  - adaptive backoff;
  - raw-stream elision dials.

  Raw response heads are kept only on probes that fired.

## Capabilities

### New Capabilities
- `desync-scanning`: the `desync` module and `temporal_desync_scan`. Covers the smuggling and splitting checks, their oracles and controls, pacing and budgets, and the output files and result.

### Modified Capabilities
- `mcp-tools`: the tool listing gains `temporal_desync_scan`, so it has 16 tools. This capability was introduced by `warden-modules` and modified by `recon-modules`, and neither is archived yet. Archive those two first.

## Impact

- **Modules:** new `modules/desync/` (`entrypoint.sh`, `worker.py`, `workflows.py`, `activities.py`, `helpers.py`). No tool binaries and no downloads.
- **Code:** `src/warden/mcp.py` (one tool, `TOOL_NAMES`, a `from_run` source map entry), and `README.md` and `modules/README.md` (tool table and module list).
- **Tests:**
  - `tests/modules/test_desync.py`, against local raw-socket lab servers:
    - a CL.0-vulnerable server;
    - a pipelining server that ignores `Content-Length`, for the control;
    - a fold-narrowing server;
    - a patched server;
    - an erratic server.
  - `tests/modules/conftest.py` (module name list);
  - `tests/test_mcp.py` (tool count, argument validation, `from_run` source).
- **Network behaviour:** desync probes send malformed requests to targets from the fleet's IPs. They can upset fragile front-ends. Use them only against authorised scope.
