# Spec Delta

## Purpose

Passive subdomain enumeration across a fleet. The `subdomains` module runs subfinder in visible worker terminals, and `temporal_subdomain_enumeration` starts it from an agent with no tool syntax to learn.

## ADDED Requirements

### Requirement: Pinned tool install
The `subdomains` module SHALL install subfinder `v2.6.7` on each machine, verified against its pinned SHA-256, unless that version is already installed. If verification fails, the entrypoint SHALL exit non-zero without starting a worker.

#### Scenario: Fresh machine
- **WHEN** `subdomains` is activated on a machine without subfinder
- **THEN** subfinder `v2.6.7` is installed and the worker starts polling `<fleet_id>.subdomains`

#### Scenario: Checksum mismatch
- **WHEN** the downloaded subfinder does not match the pinned SHA-256
- **THEN** the slot's tab shows the mismatch and the exit code, and no worker polls from that slot

### Requirement: Input normalisation
The tool SHALL accept a list of root domains, 1 to 10,000 after normalisation. It SHALL trim, lowercase and deduplicate entries, and drop blank lines and lines starting with `#`. Entries that are not hostnames SHALL be rejected before a run starts, with up to ten of them named in the error.

#### Scenario: Messy input
- **WHEN** the tool is called with `[" Example.COM ", "example.com", "", "# note"]`
- **THEN** the run is started with the single domain `example.com`

#### Scenario: Invalid entry
- **WHEN** the input contains `https://example.com/path`
- **THEN** no run starts, and the error names that entry

### Requirement: Fan-out across the fleet
The run SHALL split the domains into chunks of at most `chunk_size` domains (default 25). Each chunk SHALL be enumerated by one worker of the fleet's `subdomains` module, and each worker SHALL enumerate at most one chunk at a time.

#### Scenario: Two machines share the work
- **WHEN** 100 domains are enumerated on a two-machine fleet with one worker per machine
- **THEN** 4 chunks are created and both machines enumerate chunks

### Requirement: Visible tool output
While a worker enumerates a chunk, subfinder's output SHALL stream into that worker's tab. A line naming the run and the chunk SHALL come first, and a line with the chunk's outcome and count SHALL come last.

#### Scenario: Operator watches a chunk
- **WHEN** a chunk is being enumerated on `wf-<fleet_id>-01`
- **THEN** the `subdomains #1` tab on that machine shows the run id, chunk number and subfinder's live output

### Requirement: Chunk retries and timeout
- **Attempts:** a chunk whose subfinder exits non-zero, or whose worker is lost, SHALL be retried on any worker, up to 2 attempts in total.
- **Timeout:** a chunk SHALL time out after 30 minutes.
- **Lost workers:** a lost worker SHALL be detected within 1 minute.

#### Scenario: Worker lost mid-chunk
- **WHEN** the terminal running a chunk is killed
- **THEN** within about a minute the chunk is retried on another worker

### Requirement: Run result
The run SHALL return:
- the subdomains found, lowercase, deduplicated and sorted;
- their count;
- the number of chunks in total, succeeded and failed;
- for each failed chunk, its domains and last error.

The run SHALL complete when at least one chunk succeeded, and SHALL fail when every chunk failed.

#### Scenario: Live enumeration
- **WHEN** `example.com` is enumerated on a `READY` fleet
- **THEN** the run completes with a count greater than 0 and every subdomain ends with `.example.com`

#### Scenario: Some chunks fail
- **WHEN** one of four chunks fails after its retries
- **THEN** the run completes, reporting 3 succeeded and 1 failed chunk with its domains and error

### Requirement: Cancellation stops tools
Cancelling a run SHALL stop its in-progress subfinder processes within one minute, and the run SHALL end `CANCELED`.

#### Scenario: Cancel during enumeration
- **WHEN** a run is cancelled while two chunks are in progress
- **THEN** both subfinder processes have exited within a minute, and the workers' tabs show the chunks as cancelled
