# Spec Delta

## Purpose

Recon output is far larger than a Temporal payload. Modules return it as files, chunk by chunk, and Warden assembles the files of a run on the operator's disk, where agents and people read them and later runs reuse them as input.

## ADDED Requirements

### Requirement: Files returned by module activities
A module activity MAY return named text files with its result.
- **Names:** a file name SHALL match `^[a-z0-9][a-z0-9._-]{0,63}$` and SHALL NOT be `result.json`.
- **Size:** each activity result SHALL stay under 1.5 MB after compression, and the activity SHALL fail with a non-retryable error naming `chunk_size` when it would not.

The workflow's own result SHALL stay a summary without file contents.

#### Scenario: Chunk with output
- **WHEN** a webs chunk probes 100 hosts and 60 answer
- **THEN** its activity returns `webs.jsonl`, `webs.txt` and `urls.txt` with 60 lines each, and the run's result holds only counts

#### Scenario: Oversized chunk
- **WHEN** one chunk's compressed files would exceed 1.5 MB
- **THEN** the chunk fails without retry, with an error asking for a smaller `chunk_size`

### Requirement: Collection into a run folder
When a run's result is fetched, Warden SHALL assemble into `state/results/<fleet_id>-<module>-<suffix>/` the files of every activity that completed in the run or its child workflows. Same-name files SHALL be concatenated in completion order without duplicate lines; `.txt` files SHALL be sorted. Fetching again SHALL rewrite the same content.

#### Scenario: Files from several chunks
- **WHEN** a run with four completed chunks is fetched
- **THEN** its folder holds each file once, holding the lines of all four chunks

#### Scenario: Retried chunk
- **WHEN** a chunk failed once and succeeded on its second attempt
- **THEN** only the successful attempt's files are in the folder

### Requirement: Partial files of unsuccessful runs
For a run that ended `FAILED`, `CANCELED`, `TERMINATED` or `TIMED_OUT`, the result tools SHALL still assemble the files of the chunks that completed, and SHALL report them with the run's status and failure.

#### Scenario: Cancelled after some chunks
- **WHEN** a run is cancelled after 3 of 10 chunks completed
- **THEN** `temporal_result` reports state `CANCELED` and the files holding those 3 chunks' output

### Requirement: Files in result tools
The result tools SHALL report the run folder and, for each file, its absolute path, line count, size in bytes and, for `.txt` files, its first 10 lines.

#### Scenario: Agent reads a result
- **WHEN** an agent fetches a completed web probe
- **THEN** it sees the paths of `webs.jsonl`, `webs.txt` and `urls.txt`, their line counts, and the first lines of `webs.txt` and `urls.txt`

### Requirement: Runs as input of other runs
A tool that accepts `from_run` SHALL read its targets from that run's output file, assembling the run folder first if needed. It SHALL refuse the call when:
- the run is not `COMPLETED`;
- the run belongs to a module the tool does not accept as a source;
- the file is empty.

The tool SHALL accept either `from_run` or explicit targets, not both.

#### Scenario: Probe the output of an enumeration
- **WHEN** an agent calls `temporal_web_probe(from_run="run/ab12cd34/subdomains-1a2b3c")` after that run completed with 1,200 subdomains
- **THEN** a web probe of those 1,200 hosts starts

#### Scenario: Wrong source
- **WHEN** an agent calls `temporal_nuclei_scan(from_run=<a subdomains run>)`
- **THEN** the call fails, stating that nuclei takes its targets from a `webs` run
