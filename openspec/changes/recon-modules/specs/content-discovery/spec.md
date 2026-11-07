# Spec Delta

## Purpose

Find exposed sensitive paths on live web services with ffuf and the leaky-paths wordlist, from the `ffuf` module through `temporal_web_fuzzing`, writing results as reconftw does.

## ADDED Requirements

### Requirement: Web fuzzing tool
`temporal_web_fuzzing` SHALL accept either targets (see `module-runs`) or `from_run`, a completed `webs` run whose `urls.txt` supplies the URLs. It SHALL also accept an optional fleet id and a `chunk_size` (default 5). It SHALL start a run of the `ffuf` module and return its run id at once.

#### Scenario: Fuzz a probe's output
- **WHEN** an agent calls `temporal_web_fuzzing(from_run=<completed webs run>)`
- **THEN** an `ffuf` run starts over that run's live URLs

### Requirement: Leaky-paths wordlist
The module SHALL fuzz with `leaky-paths.txt` from `ayoubfathi/leaky-paths` at a pinned commit, verified against a pinned SHA-256. ffuf SHALL fuzz `<target URL without its trailing slash>/FUZZ` with each entry's leading slash removed, so that auto-calibration's probes reach the same host as the entries.

#### Scenario: Path joining
- **WHEN** `https://example.com/` is fuzzed with the entry `/.git/config`
- **THEN** ffuf requests `https://example.com/.git/config`

### Requirement: reconftw ffuf behaviour
For each URL, ffuf SHALL:
- match every status except 404, with auto-calibration;
- stop on spurious errors;
- send a browser User-Agent;
- run non-interactively;
- stop after at most 900 seconds.

URLs in a chunk SHALL be fuzzed one after another. A chunk SHALL time out after 2 hours.

#### Scenario: Wildcard site
- **WHEN** a site answers 200 with the same page for every path
- **THEN** auto-calibration filters those responses and the site contributes no false positives

### Requirement: Fuzzing output
Each run SHALL produce:
- `ffuf.jsonl`: one ffuf JSON result per line;
- `ffuf.txt`: one line per result, `<status> <length> <url>`, sorted.

The run result SHALL report the number of URLs fuzzed and results found.

#### Scenario: Exposed git config
- **WHEN** `https://example.com/.git/config` answers 200 with 92 bytes
- **THEN** `ffuf.txt` contains `200 92 https://example.com/.git/config`, and `ffuf.jsonl` holds its full JSON result
