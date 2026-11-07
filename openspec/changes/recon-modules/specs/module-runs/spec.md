# Spec Delta

## Purpose

The behaviour shared by the web recon modules (`webs`, `nuclei`, `ffuf`): how targets are given, split across the fleet, shown in the workers' tabs, retried, timed out and cancelled.

## ADDED Requirements

### Requirement: Web targets
`temporal_web_probe` and `temporal_nuclei_scan` SHALL accept hostnames, IPv4 addresses, `host:port` and `http`/`https` URLs; `temporal_web_fuzzing` SHALL accept `http`/`https` URLs only. The tools SHALL trim entries, lowercase hostnames and schemes, drop blank lines and lines starting with `#`, and deduplicate. Other entries SHALL be rejected before a run starts, with up to ten of them named in the error.

#### Scenario: Mixed input
- **WHEN** the tool is called with `["Example.com", "https://Example.com:8443/app", "example.com", "# x"]`
- **THEN** the run starts with `example.com` and `https://example.com:8443/app`

#### Scenario: Invalid entry
- **WHEN** the input contains `ftp://example.com`
- **THEN** no run starts, and the error names that entry

#### Scenario: Bare host for fuzzing
- **WHEN** `temporal_web_fuzzing` is called with `example.com`
- **THEN** no run starts, and the error asks for URLs (for example from a web probe)

### Requirement: Web target limits
A run SHALL take 1 to 50,000 targets after normalisation. Input that would serialise to more than 1.5 MB SHALL be rejected, with an error asking to split it.

#### Scenario: Too many targets
- **WHEN** the tool is called with 60,000 hostnames
- **THEN** no run starts, and the error states the 50,000 limit

### Requirement: Chunked fan-out
A run SHALL split its targets into chunks of at most `chunk_size` (1 to 1,000; each tool sets its own default). Each chunk SHALL be processed by one worker of the module on the fleet, one chunk at a time per worker.

#### Scenario: Two machines share a scan
- **WHEN** 100 URLs are scanned with `chunk_size=25` on a two-machine fleet with one worker each
- **THEN** 4 chunks are created and both machines process chunks

### Requirement: Visible tool output
While a worker processes a chunk, its tab SHALL show:
1. a line naming the run, the chunk and the attempt;
2. one human-readable line per result as it is found, plus the tool's own progress messages;
3. a final line with the chunk's outcome and count.

#### Scenario: Operator watches a chunk
- **WHEN** a nuclei chunk runs on `wf-<fleet_id>-01`
- **THEN** the `nuclei #1` tab shows the run id, the chunk number, each finding as it appears, and the outcome

### Requirement: Retries, timeouts and lost workers
- **Attempts:** a chunk whose tool exits non-zero, or whose worker is lost, SHALL be retried on any worker, up to 2 attempts in total.
- **Lost workers:** a lost worker SHALL be detected within 2 minutes.
- **Timeouts:** each module SHALL set a per-chunk timeout.
- **Run outcome:** the run SHALL complete when at least one chunk succeeded, and SHALL fail when every chunk failed.

A completed run SHALL report the chunks in total, succeeded and failed, and each failed chunk's targets and last error.

#### Scenario: One chunk fails
- **WHEN** one of four chunks fails after its retries
- **THEN** the run completes, reporting 3 succeeded and 1 failed chunk with its targets and error

### Requirement: Surviving network blips
A worker that loses its link to Temporal for less than about 100 seconds SHALL keep its chunk or step: the work is not retried.

#### Scenario: Short tailnet outage
- **WHEN** a worker's link to Temporal drops for 30 seconds while a 25-minute step finishes
- **THEN** the step's result is recorded once, and the step is not run again

### Requirement: Cancellation stops tools
Cancelling a run SHALL stop its in-progress tool processes, including their child processes, within one minute. The run SHALL end `CANCELED`.

#### Scenario: Cancel a scan
- **WHEN** a run is cancelled while two chunks are in progress
- **THEN** both tool processes have exited within a minute, and the tabs show the chunks as cancelled

### Requirement: Pinned tools
Each module SHALL install its tool binary at a pinned version, verified against a pinned SHA-256, unless that version is already installed. Pinned wordlists SHALL be verified the same way. On a mismatch, the entrypoint SHALL exit non-zero without starting a worker.

#### Scenario: Checksum mismatch
- **WHEN** a downloaded binary does not match its pinned SHA-256
- **THEN** the slot's tab shows the mismatch and the exit code, and no worker polls from that slot
