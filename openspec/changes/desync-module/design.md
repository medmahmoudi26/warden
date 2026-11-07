# Design

## Context

Warden modules are self-contained folders (see `modules/README.md`). Each has an `entrypoint.sh`, a Temporal worker that polls `<fleet>.<module>`, and activities that return gzipped files under `warden_files`. Every existing module wraps a binary (httpx, nuclei, ffuf) through `helpers.run_tool`.

The method comes from an earlier Go scanner. Its core is a socket primitive it owns: it writes exact bytes and attributes the answers per step. On top of that sit:
- a technique family for CL.0;
- a fold-vector generator;
- oracles;
- runtime plumbing: units, datasets, Nexus, the corpus and the reach oracle.

Only the first three carry the method. See proposal.md for what is kept and dropped. See `specs/desync-scanning` for the behaviour.

## Goals / Non-Goals

**Goals:**
- One readable Python module that ports the scanner's oracles faithfully: pipelined connection, stability gate, matched controls that only withdraw, detection-only payloads, per-host pacing, announced gaps.
- Testable on a laptop against lab servers whose ground truth is known, as the scanner's own tests were.

**Non-Goals:**
- Byte-for-byte parity with the scanner's row schema, or with its corpus identities (`request_hex`).
- Choosing which point to inject from crawled responses (CORS, `Vary`, `Set-Cookie`), and the header, cookie and JSON delivery contexts.
- HTTP/2, the h2→h1 downgrade family, CL.TE and TE.CL.

## Decisions

**Pure Python on raw asyncio sockets, not the Go binary.** The scanner's HTTP library bought nothing on the read path, and it sidestepped the library entirely by owning the socket. Owning a socket is a few lines with `asyncio.open_connection`.
- TLS uses `ssl.SSLContext` with `check_hostname=False`, `CERT_NONE` and ALPN `["http/1.1"]`. SNI is sent unless the host is an IP.
- This keeps the module like the others: `uv run --script worker.py`, `temporalio` only, nothing to build or verify on the fleet.
- *Alternative:* build `foldscan` with Go on the fleet, or ship a binary. Rejected: it adds a Go toolchain install or a binary pin, and the code we would call is the 80% we are not porting.

**Layout follows the contract.**
- `workflows.py` holds `DesyncScan` and `DesyncInput`.
- `activities.py` holds `scan_chunk` and `ScanChunk`. It runs one URL's baseline, then its checks, and emits rows.
- `helpers.py` holds everything else:
  - the socket primitive (`exchange`);
  - the response-stream parser;
  - the per-host pacer;
  - the smuggling family and seeds;
  - the fold vectors;
  - the two analyses;
  - row and line formatting;
  - `pack`, the codec, and the workflow-side error helpers copied from `webs`.

  The file is long, but each part is a small pure function that the tests import directly.

**The socket primitive writes everything, then reads.** `exchange(target, [raw, ...], timeout)` opens one connection and writes all requests in one `write`. It then reads until one of these happens:
- the stream holds as many complete answers as requests, plus a 0.5 s grace period for extra bytes;
- the peer closes the connection;
- the read times out (10 s).

It returns the raw bytes, the parsed answers in order, the trailing bytes, the step after which the peer closed, and any connect or TLS error. Raw bytes are kept before parsing: a parse failure with bytes present is recorded as a signal, not discarded.

The parser handles:
- `Content-Length`, `Transfer-Encoding: chunked` and read-until-close bodies;
- 1xx interim answers, which are skipped;
- 204 and 304 answers, which have no body.

**One baseline per URL, shared by both checks.** The benign request is `GET <path>?cb=<random> HTTP/1.1`, `Host`, `Connection: keep-alive`, generated once per URL. It is the baseline and also the pre and post steps of every smuggling probe, so the post step is byte-identical to the pre step.

The stability gate (3 repeats, same status and body SHA-256) also gates the split check. That check compares arms against each other, not against the baseline. A URL that cannot reproduce its own answer would still make arm-against-arm differences meaningless.

**Smuggling family as data.** Seeds, positions, casings and hostile bytes are ported from the scanner's family table. Members are rendered at expansion time as `latin-1` strings with `${host}`, `${endpoint}`, `${random}` and `${cl}` slots, deduplicated by the rendered template, and seeds sort first. `${cl}` is bound to the real body length on each send, as the scanner does. The control replaces the gadget line with `<name>: <len>` and keeps the same body and canary.

**Split: path only, and screen and confirm in one request pair.** In the scanner, the screen compares a mutated request with the baseline, and only escalates to the control and a canary confirmation when something moved. Here every probe is a pair from the start:
- the fold arm, `<path><v>Host:<canary><v>`;
- the control arm, `<path><c>Host:<canary><c>`.

It costs 2 requests per vector. The scanner costs 1 to 4, and the comparison it relies on to avoid false positives, against the matched control, is always present.

Vectors are generated as in the scanner, for the `lf` and `cr` classes only (no `crlf`). Redaction follows the scanner's: in the body only, on both arms, it removes the encoded, decoded JSON-escaped (`\u070a`) and folded forms of both the vector and its control.

The canary is compared per place: after `://`, in a header, or in the body. It counts as reflected where the fold arm has it and the control arm does not. A whole-answer comparison would withdraw real leads, because the control arm's 404 page usually echoes its own path, and that path carries the canary. The scanner's confirmation step makes the same exception for `://`.

**Concurrency and pacing inside a chunk.** `scan_chunk` probes up to 8 URLs at once with `asyncio`, and every connection goes through one per-host pacer: an `asyncio.Lock` and a next-allowed time per host. Because the workflow sorts URLs by `(host, port, url)` before chunking, one host's URLs normally share a chunk, and so share a worker and a pacer. A host split across two chunks on two workers can see up to twice the rate; this is accepted.

**Budget and heartbeats.**
- Each URL runs under `asyncio.timeout(budget)`. On expiry, the URL's partial rows are kept and a `capped` row is added, with the probes sent and planned.
- A background task heartbeats every 10 s with progress counts, so cancellation is delivered and lost workers are detected.
- The chunk's `start_to_close` timeout is `ceil(len(chunk) / 8) × budget + 10 min`.
- Retries are as in `webs`: 2 attempts, and `OutputTooLarge` is not retried.

**Output stays small.** A raw answer head (4 KB) and the request text are kept only on probes with a signal. A quiet probe is about 600 bytes of JSON before gzip. At the default chunk size of 20 URLs (5 smuggle and 4 split probes each), a chunk returns well under 100 KB. `pack` refuses to send more than 1.5 MB and names `chunk_size`.

**MCP tool.** `temporal_desync_scan` reuses `web_targets(..., URL_SOURCES, urls_only=True)` and `check_chunk_size`. It validates `checks`, `tier` and `rate_ms`, then calls `runs.start_run(module="desync", workflow="DesyncScan", arg=...)`. Nothing else in `src/warden` changes.

**Tests run against a lab, not mocks.** `tests/modules/desync_lab.py` is an asyncio raw-TCP server. It records every byte it receives and every connection time, and has these modes:

| Mode | Behaviour |
|---|---|
| `cl0` | ignores a malformed `Content-Length` line, so its body prefixes the next request; honours a well-formed one |
| `pipeline` | ignores every `Content-Length`; the control must withdraw |
| `fold` | percent-decodes the path, narrows each codepoint to its low byte, splits the request at the resulting newline, and redirects to `http://<Host>/` |
| `patched` | 404 page echoing the decoded path, with no narrowing |
| `erratic` | random statuses |

The workflow tests run on the dev server fixture, as the other modules' do.

## Risks / Trade-offs

- **Dynamic pages fail the gate.** A page with CSRF tokens or timestamps changes its body digest, so the URL is refused as erratic, as the scanner does. → It is listed in `skipped.txt` with the reason, so the gap is visible. Relaxing the gate is an open question.
- **Fragile front ends.** Malformed requests can trip WAF bans or upset fragile servers. → 1 connection per second per host by default, detection-only payloads, and an explicit authorised-scope warning in the tool description and README.
- **Path-only splitting misses most delivery contexts.** Header values, cookies and JSON bodies are not tested. → This is accepted for v1; the vector table already carries `wire_bytes` and `json_escape`, so other contexts can be added later.
- **No HTTP/2.** Hosts that require h2 fail the TLS handshake or answer h1 badly. → They are listed as unreachable or erratic.
- **Pipelined answers through CDNs.** Many CDNs answer each pipelined request from a separate backend connection, so most targets show nothing. That is the correct outcome, not a bug.

## Migration Plan

There is nothing to migrate. Roll out by restarting `warden-serve` and reconnecting the MCP client so `temporal_desync_scan` appears. The module folder ships on its first `module_activate` or first run. Roll back by removing the tool and the folder.

## Open Questions

- Should the stability gate fall back to status plus body length for pages with dynamic content? This can be decided after the first real runs show the share of erratic URLs, without changing the checks.
