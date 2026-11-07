# Tasks

## 1. Lab and wire primitives

- [x] 1.1 Add `tests/modules/desync_lab.py`, an asyncio raw-TCP lab server with modes `cl0`, `pipeline`, `fold`, `patched` and `erratic`, as described in design.md. It records every received byte and every connection time. Verify it with a smoke test in `tests/modules/test_desync.py` that sends a hand-written pipelined request to each mode and checks the documented answer.
- [x] 1.2 Add `modules/desync/helpers.py`'s wire layer:
  - `exchange`: one connection, every request written before any read, then read until N answers plus grace, close or timeout; TLS with ALPN `http/1.1` and no verification;
  - the response-stream parser: Content-Length, chunked, until-close, 1xx, 204/304, trailing bytes, parse errors with raw bytes kept;
  - the per-host pacer.

  Verify with unit tests for:
  - the parser: two stacked answers, a chunked answer, garbage after an answer, an unparseable stream;
  - byte-exactness: a hostile header line arrives at the lab verbatim;
  - pacing: three connections to one host are at least `rate_ms` apart.

## 2. Techniques and vectors

- [x] 2.1 Port the smuggling family into `helpers.py`:
  - the 5 seeds with their report ids;
  - 7 positions × hostile bytes × 3 casings, deduplicated, with well-formed lines dropped and seeds first;
  - rendering with the correct body length (`${cl}`) and the matched control line.

  Verify with unit tests:
  - the default set is exactly the 5 seed lines;
  - `before-value-2b-preserve` cites its reports;
  - no member contains 0x00 or is `Content-Length: <n>`;
  - the control differs from the attack only in the header line;
  - every body ends without a blank line and names no host.
- [x] 2.2 Port the fold vectors (`lf` and `cr`; families raw, overlong, double and fold; tiers 1 to 3; controls one codepoint higher; screen = raw plus the first published fold per class) and the body redaction. Verify with unit tests:
  - tier 1 holds U+0B0A, U+560A and U+1F60A and no other 3- or 4-byte fold;
  - each control has the same length as its vector;
  - the screen is 4 vectors;
  - redaction removes both arms' encoded, decoded and folded forms from the body only.

## 3. Checks, activity and workflow

- [x] 3.1 Implement the analyses in `helpers.py`:
  - the stability gate;
  - smuggling: void, signals, and control withdrawal;
  - splitting: arm-against-arm signals, canary location, and the lead rule;
  - row and line formatting for `desync.jsonl`, `desync.txt` and `skipped.txt`.

  Verify with unit tests on hand-built answers: a pre-flight mismatch is void, a lone closed-after-attack counts nothing, a raw vector's status-only signal is not a lead.
- [x] 3.2 Add `modules/desync/activities.py` `scan_chunk`:
  - per-URL baseline, then the checks, with up to 8 URLs at once;
  - smuggle seeds or the full family;
  - split screen with escalation, or a sweep;
  - the per-URL budget with a `capped` row;
  - heartbeats every 10 s;
  - tab lines for the chunk, leads, skipped URLs and the outcome;
  - packed files.

  Verify with `tests/modules/test_desync.py` against the lab modes:
  - `cl0` gives smuggle leads that survive their control;
  - `pipeline` gives none, and every signal is withdrawn;
  - `fold` gives a split lead with the canary in the `authority`;
  - `patched` gives no lead;
  - `erratic` is skipped with its statuses;
  - an unreachable port is skipped;
  - a tiny budget gives a `capped` line.
- [x] 3.3 Add `modules/desync/workflows.py` `DesyncScan`: URLs sorted by host and port, chunks, `start_to_close = ceil(n/8) × budget + 10 min`, 2 attempts, `OutputTooLarge` not retried, a counts-only result. Verify on the dev server fixture:
  - a two-host run assembles `desync.jsonl`, `desync.txt` and `skipped.txt` through `collect_files`;
  - interleaved hosts are chunked by host;
  - cancellation ends the run `CANCELED` and closes the lab connections within a minute.
- [x] 3.4 Add `modules/desync/worker.py` and `entrypoint.sh`. The entrypoint has a first-line description, no installs, and `exec uv run --script worker.py`. Add `desync` to `NAMES` in `tests/modules/conftest.py`. Verify with `tests/modules/test_entrypoints.py` (discovery, four files, executable entrypoint, `bash -n`) and `tests/test_codec.py`.
- [x] 3.5 Add `desync` to `modules/README.md`'s module list. Verify by reading the rendered section.

## 4. MCP tool

- [x] 4.1 Add `temporal_desync_scan` to `src/warden/mcp.py`:
  - targets or `from_run` (`webs` → `urls.txt`), URLs only;
  - `checks`, `sweep`, `tier`, `rate_ms` and `chunk_size`, each validated;
  - a call to `runs.start_run(module="desync", workflow="DesyncScan")`;
  - an entry in `TOOL_NAMES`.

  Verify with `tests/test_mcp.py`: the tool count is 16, the arguments reach `start_run`, and a bare host, an unknown check, a tier of 4 and a `rate_ms` of 100 are each rejected.
- [x] 4.2 Document the tool in `README.md` (tool table row, chaining line, active-scanning note). Verify that the row matches the tool's signature and output files.

## 5. Integration

- [x] 5.1 Run the whole suite with `.venv/bin/python -m pytest -q`; verify it passes with no new skips other than those already present.
- [x] 5.2 Run `openspec validate desync-module --strict`; verify that it passes.

## Workflow follow-up

- After a restart of `warden-serve` and an MCP reconnect, an optional live check: `fleet_up` with one machine, then `temporal_desync_scan` against an authorised test target, then `temporal_result`, then `fleet_down`.
- Archive `warden-modules` and `recon-modules` before this change, since `mcp-tools` is modified here.
