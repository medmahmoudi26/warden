# Spec Delta

## Purpose

Probe live web URLs for HTTP desync bugs: CL.0 request smuggling through obfuscated `Content-Length` header lines, and CRLF request splitting through lossy Unicode narrowing in the path. This runs from the `desync` module through `temporal_desync_scan`, and records observations, matched controls and coverage gaps rather than verdicts.

## ADDED Requirements

### Requirement: Desync scan tool
`temporal_desync_scan` SHALL accept either `http`/`https` URLs, normalised and limited as web targets are (see `module-runs`), or `from_run`: a completed `webs` run whose `urls.txt` supplies the URLs. It SHALL start a run of the `desync` module and return its run id at once.

#### Scenario: Scan a web probe's output
- **WHEN** an agent calls `temporal_desync_scan(from_run=<completed webs run>)`
- **THEN** a `desync` run starts over that run's live URLs with both checks

#### Scenario: Bare host
- **WHEN** an agent calls `temporal_desync_scan(targets=["example.com"])`
- **THEN** no run starts, and the error asks for http(s) URLs

### Requirement: Desync scan options
`temporal_desync_scan` SHALL accept:
- `checks`, a non-empty subset of `smuggle` and `split` (default both);
- `sweep` (default false);
- `tier`, 1 to 3 (default 1);
- `rate_ms`, 250 to 60,000 (default 1,000);
- an optional fleet id;
- `chunk_size`, 1 to 1,000 (default 20).

Out-of-range values SHALL be rejected before a run starts.

#### Scenario: Only one check
- **WHEN** an agent calls `temporal_desync_scan(targets=["https://example.com/"], checks=["smuggle"])`
- **THEN** the run probes only smuggling techniques

#### Scenario: Unknown check
- **WHEN** an agent calls `temporal_desync_scan(targets=["https://example.com/"], checks=["te-cl"])`
- **THEN** no run starts, and the error names the accepted checks

### Requirement: Host-grouped chunks
The run SHALL order its URLs by host and port before splitting them into chunks of at most `chunk_size`. Chunks SHALL be processed as the web recon modules process theirs (see `module-runs`): one chunk at a time per worker, a line per chunk, lead and skipped target in the worker's tab, 2 attempts, and lost workers detected within 2 minutes.

#### Scenario: One host's URLs stay together
- **WHEN** a run gets 6 URLs, 3 on `a.example.com` and 3 on `b.example.com`, interleaved, with `chunk_size=3`
- **THEN** one chunk holds the three `a.example.com` URLs and the other the three `b.example.com` URLs

### Requirement: Exact bytes on the wire
Every probe request SHALL reach the target's socket byte for byte as built. Header order, header case, whitespace, bare CR/LF bytes and non-ASCII bytes SHALL NOT be changed. HTTPS targets SHALL be reached over TLS that offers only `http/1.1` and does not verify certificates.

#### Scenario: Hostile header line
- **WHEN** the smuggle check sends the technique `Content-Length\t:\t<n>`
- **THEN** the target receives a header line with a tab before and after the colon

### Requirement: Per-host pacing
A worker SHALL open at most one connection to a given host per `rate_ms`, across all URLs of its chunk.

#### Scenario: Three URLs on one host
- **WHEN** a chunk holds three URLs on one host and `rate_ms` is 1,000
- **THEN** no two connections to that host start less than one second apart

### Requirement: Stability gate
Before probing a URL, the worker SHALL send its benign request (a `GET` of the URL's path with a random cache-busting query) three times, each on a new connection. It SHALL probe the URL only if all three answers have the same status and the same body digest. Otherwise it SHALL send no probe and record the URL as skipped, with the statuses seen.

#### Scenario: Erratic target
- **WHEN** a target answers its benign request with 200, 503 and 200
- **THEN** no probe is sent to it, and `skipped.txt` lists it as erratic with those statuses

#### Scenario: Unreachable target
- **WHEN** no connection to a target can be opened
- **THEN** no probe is sent to it, and `skipped.txt` lists it as unreachable with the connection error

### Requirement: Smuggling probe
For each smuggling technique, the worker SHALL write three requests on one connection before reading any answer:
1. the benign request;
2. the attack;
3. the same benign request again.

The attack SHALL be a `POST` whose `Content-Length` line is obfuscated by the technique, while still carrying the body's correct length. Its body SHALL be an incomplete request for a random path (the canary), with no terminating blank line.

#### Scenario: Pipelined probe
- **WHEN** a technique is probed against a target
- **THEN** the target receives benign, attack and benign requests on one connection, and the attack's body ends without a blank line

### Requirement: Void smuggling probes
A smuggling probe SHALL be void, with no signal and no lead, when the first benign answer on its connection differs from the baseline in status or body digest, or when the connection could not be opened.

#### Scenario: Pre-flight mismatch
- **WHEN** the first benign answer on the probe's connection has a different status from the baseline
- **THEN** the probe is recorded as void and is not a lead

### Requirement: Smuggling signals
A smuggling probe that is not void SHALL record which of these fired:
- the third answer's status or body differs from the baseline;
- the third answer could not be parsed;
- no third answer arrived, although the attack was not rejected with a status of 400 or more;
- bytes arrived after the third answer;
- the canary appeared in any answer.

A connection closed after the attack SHALL count only alongside another signal.

#### Scenario: CL.0 target
- **WHEN** a target's back end ignores the obfuscated `Content-Length` and reads the body as the start of the next request
- **THEN** the probe records that the third answer differs from the baseline

### Requirement: Smuggling control
When a smuggling probe records any signal, the worker SHALL repeat it on a new connection with a well-formed `Content-Length` line in place of the obfuscated one. The body and canary SHALL stay the same. If the control also fires, every signal of the probe SHALL be withdrawn: the observation is marked as withdrawn by its control and is not a lead.

#### Scenario: Server ignores Content-Length entirely
- **WHEN** a target reads the body as a request even with a correct, well-formed `Content-Length`
- **THEN** every technique's signal is withdrawn by its control, and the target has no smuggling lead

#### Scenario: Lead survives its control
- **WHEN** a target misreads only the obfuscated `Content-Length` line
- **THEN** the technique is a lead, listed in `desync.txt` with its header line and signals

### Requirement: Smuggling techniques
By default, the smuggle check SHALL send the five distinct header lines from paid reports:
- `\tContent-Length: <n>`;
- `Content-Length\t:\t<n>`;
- `Content-Length \r\n : <n>`;
- `Content-Length: +<n>`;
- `content-length: +<n>`.

With `sweep`, it SHALL send these first, then every other member of the family.

#### Scenario: Default techniques
- **WHEN** the smuggle check runs without `sweep` on a stable target
- **THEN** exactly five techniques are probed

### Requirement: Smuggling family
A family member SHALL be one hostile byte placed at one of seven positions in the header line: before the name, inside the name, after the name, around the colon, before the value, after the value, or as an obs-fold. The header name SHALL be in one of three casings: as written, lower or upper. Each member SHALL be named `<position>-<byte hex>-<casing>`. A member that reproduces a reported line SHALL cite that report.

#### Scenario: Seed provenance
- **WHEN** the family is expanded
- **THEN** the member `before-value-2b-preserve` renders `Content-Length: +<n>` and cites the reports it reproduces

### Requirement: Hostile bytes
The bytes placed by the smuggling family SHALL be:
- 0x01 to 0x20;
- 0x7F to 0xFF;
- the characters ``+ - . , ; " ' ( ) * / \``.

Members that render a well-formed line, or the same request as an earlier member, SHALL be left out.

#### Scenario: No NUL byte
- **WHEN** the family is expanded
- **THEN** no member contains the byte 0x00, and none is the plain line `Content-Length: <n>`

### Requirement: Splitting probe
For each fold vector, the worker SHALL send two requests, each on a new connection:
- the fold arm, a `GET` of `<path><vector>Host:<canary><vector>`;
- the control arm, the same request with the vector's control: each component's codepoint one higher, so its length and shape are unchanged.

The canary SHALL be a random name under `.probe.invalid`, the same on both arms.

#### Scenario: Matched control
- **WHEN** the vector `%DC%8A` (U+070A) is probed
- **THEN** the control arm carries `%DC%8B` (U+070B) at the same places, with the same canary

### Requirement: Splitting signals
A splitting probe SHALL compare its fold arm against its control arm. It SHALL record these signals:
- the canary appears in the fold arm's answer at a place where the control's answer does not have it: after `://` (`authority`), in a header (`header`), or in the body (`body`);
- the statuses differ;
- the bodies differ once every form of both payloads is redacted from both;
- header names appear only in the fold arm;
- the fold arm's answer cannot be parsed, or is followed by extra bytes.

#### Scenario: Narrowing back end
- **WHEN** a target decodes the path, narrows U+070A to a line feed, and builds a redirect from the injected `Host`
- **THEN** the probe records the canary in the `authority` and is a lead

#### Scenario: Control echoes the canary elsewhere
- **WHEN** the fold arm redirects to `http://<canary>/` and the control arm's 404 page echoes its path, canary included
- **THEN** the canary still counts as reflected in the `authority`, where the control does not have it

#### Scenario: Reflective 404
- **WHEN** a target echoes the requested path in a 404 page, and narrows nothing
- **THEN** both arms answer alike once redacted, and the probe records no signal

### Requirement: Splitting leads
A splitting probe SHALL be a lead when its canary was reflected, whatever the vector. It SHALL also be a lead when its vector is not a raw encoding and it records any signal. A raw-encoding probe with no reflected canary SHALL be recorded but not be a lead.

#### Scenario: Server rejects a raw line feed
- **WHEN** a target answers 400 to `%0A` and 404 to `%0B`, and reflects no canary
- **THEN** the probe records the status signal and is not a lead

### Requirement: Splitting screen and escalation
Without `sweep`, the split check SHALL first screen each URL with four vectors: for line feed and carriage return, the raw encoding and the first published fold. If any screen probe records a signal, it SHALL then send every other vector of the chosen tier. With `sweep`, it SHALL send every vector of the tier at every URL.

#### Scenario: Quiet target
- **WHEN** no screen probe on a URL records a signal
- **THEN** only the four screen vectors are sent to it

#### Scenario: Reacting target
- **WHEN** a screen probe on a URL records a signal
- **THEN** every other tier-1 vector is sent to that URL

### Requirement: Vector tiers
Splitting vectors target a line feed or a carriage return. Tier 1 SHALL hold:
- the raw encodings;
- the 2-, 3- and 4-byte overlong forms;
- the 1-, 2- and 3-level percent-encodings;
- every 2-byte fold;
- the published 3- and 4-byte folds.

Tier 2 SHALL add every 3-byte fold, and tier 3 every 4-byte fold.

#### Scenario: Published folds in tier 1
- **WHEN** the tier-1 vectors are listed
- **THEN** they include `%E0%AC%8A` (U+0B0A), `%E5%98%8A` (U+560A) and `%F0%9F%98%8A` (U+1F60A), and no other 3- or 4-byte fold

### Requirement: Per-URL time budget
Each URL SHALL have a time budget: 10 minutes, or 60 minutes with `sweep`. When the budget runs out, the worker SHALL stop probing that URL. It SHALL list the URL in `skipped.txt` as capped, with how many probes it sent of how many were planned.

#### Scenario: Sweep runs out of time
- **WHEN** a smuggle sweep at one connection per second reaches the 60-minute budget
- **THEN** probing of that URL stops, and `skipped.txt` lists it as capped with the counts sent and planned

### Requirement: Detection-only probes
Probes SHALL NOT contain a complete smuggled request. They SHALL NOT name any host other than the target and a `.probe.invalid` canary.

#### Scenario: Smuggled body
- **WHEN** any smuggling technique is rendered
- **THEN** its body is a request line and one header without a terminating blank line, and names no host

### Requirement: Observation file
Each run SHALL produce `desync.jsonl`, with one JSON object per baseline, probe and skipped URL. Each object SHALL record:
- the check, URL, and technique or vector;
- its signals, whether a control withdrew them, and whether it is a lead;
- a summary of every answer;
- the requests sent.

Raw answer heads, up to 4 KB each, SHALL be kept only on probes that recorded a signal.

#### Scenario: Quiet probe
- **WHEN** a probe records no signal
- **THEN** its object has answer summaries (status, sizes, digest, timing) but no raw answer bytes

### Requirement: Lead and skipped files
Each run SHALL produce:
- `desync.txt`, one line per lead: `<url> [<check>] [<technique or vector>] [<header line or encoding>] [<signals>]`;
- `skipped.txt`, one `<url> <reason>` line per URL not fully probed.

#### Scenario: Lead line
- **WHEN** the technique `before-value-2b-preserve` is a lead on `https://a.example.com/`
- **THEN** `desync.txt` has a line starting `https://a.example.com/ [smuggle] [before-value-2b-preserve]`

### Requirement: Desync run result
A completed run SHALL report counts, not observations:
- the URLs given, probed and skipped;
- the probes and requests sent;
- the probes that signalled;
- the signals withdrawn by controls;
- the leads per check;
- the chunks in total, succeeded and failed, and each failed chunk's URLs and error.

#### Scenario: Result shape
- **WHEN** a run over 40 URLs completes with one smuggling lead
- **THEN** its result reports 40 URLs, the probed and skipped counts, and one smuggle lead

### Requirement: Cancelling a desync run
Cancelling a run SHALL stop the in-progress probes of every chunk within one minute and close their connections. The run SHALL end `CANCELED`.

#### Scenario: Cancel mid-sweep
- **WHEN** a sweep run is cancelled while two chunks are probing
- **THEN** both chunks stop within a minute, and the run ends `CANCELED`
