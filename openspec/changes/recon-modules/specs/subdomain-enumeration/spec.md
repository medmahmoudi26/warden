# Spec Delta

## ADDED Requirements

### Requirement: Provider configuration
When the module folder holds `config/provider-config.yaml`, subfinder SHALL use it. That file SHALL be shipped to the fleet with the module, and SHALL NOT be committed to git. Subfinder SHALL query all of its sources, with a time limit of `passive_minutes` per domain (default 180, reconftw's).

reconftw's extra passive source (`ip.thc.org`) SHALL also be queried, and its failures ignored.

#### Scenario: API keys in use
- **WHEN** `modules/subdomains/config/provider-config.yaml` holds API keys and a run starts
- **THEN** subfinder on the fleet runs with that file, and sources needing keys contribute results

#### Scenario: No provider file
- **WHEN** the file is absent
- **THEN** subfinder runs with its keyless sources only

### Requirement: DNS resolution
Every candidate name SHALL be resolved before it counts as found:
- the passive results;
- the root domain itself;
- the brute-force and permutation candidates.

Resolution SHALL use public resolvers (refreshed when the module starts, at most once a day) with validation against a pinned list of trusted resolvers, and SHALL filter wildcard answers. Rates:
- 5,000 queries per second to public resolvers;
- 400 to trusted resolvers;
- 30 wildcard tests;
- a wildcard batch of 1,500,000.

#### Scenario: Stale passive name
- **WHEN** subfinder reports `old.example.com`, which no longer resolves
- **THEN** it appears in `passive.txt` but not in `subdomains.txt`

#### Scenario: Wildcard zone
- **WHEN** `*.example.com` resolves to one IP
- **THEN** brute-forced names that only match the wildcard are not reported

### Requirement: DNS brute force
When `bruteforce` is on (the default), each domain SHALL be brute-forced with reconftw's subdomain wordlist (pinned commit and SHA-256). With `deep`, n0kovo's huge list (pinned) SHALL be used instead. The resulting names SHALL be resolved again before they are added.

#### Scenario: Brute force finds a name
- **WHEN** `vpn.example.com` resolves but no passive source knows it
- **THEN** it is in `subdomains.txt` and counted under `bruteforce`

### Requirement: Wordlist permutations
When `permutations` is on (the default), each domain SHALL get two rounds of gotator permutations with reconftw's flags, each capped at 2 GB and resolved; round two permutes round one's resolved names.
- **Input:** resolved names if at most 500 (or `deep`), else unresolved passive names if at most 1,500, else skipped.
- **Wordlist:** reconftw's full list for at most 100 names (or `deep`), its short list above.

#### Scenario: Small domain
- **WHEN** a domain has 40 resolved names
- **THEN** gotator runs on them with the full list, and new resolved permutations are counted under `permutations`

#### Scenario: Very large domain
- **WHEN** a domain has 5,000 resolved and 20,000 unresolved passive names, without `deep`
- **THEN** the gotator step is skipped and the domain's counts say so

### Requirement: Regex permutations
When `permutations` is on, regulator SHALL learn regex patterns from each domain's resolved names, generate candidates from them, and resolve the candidates. Above 2,000 resolved names the step SHALL be skipped and noted, because regulator's memory grows with the square of its input.

#### Scenario: Patterned names
- **WHEN** a domain has `api-dev-1`, `api-dev-2` and `api-prod-1` resolved and `api-prod-2` exists
- **THEN** regulator generates `api-prod-2`, it resolves, and it is counted under `regex`

### Requirement: Enumeration options
`temporal_subdomain_enumeration` SHALL accept:
- `bruteforce` (default true);
- `permutations` (default true);
- `deep` (default false);
- `passive_minutes` (1 to 1,440, default 180), subfinder's time limit.

#### Scenario: Bounded passive step
- **WHEN** an agent calls the tool with `passive_minutes=15` for a domain whose keyed sources page slowly
- **THEN** subfinder stops after 15 minutes with what it found, and the next steps run

#### Scenario: Passive and resolution only
- **WHEN** an agent calls the tool with `bruteforce=false, permutations=false` for 100 domains
- **THEN** each domain runs only its passive and resolve steps

## MODIFIED Requirements

### Requirement: Pinned tool install
The `subdomains` module SHALL install its tools on each machine, each skipped when already present at its pinned version:
- **Checked against a pinned SHA-256:** subfinder `v2.17.0`, puredns `v2.1.1`, the wordlists and the trusted resolvers;
- **From apt:** massdns, Go and a C toolchain;
- **gotator:** with `go install` at a pinned commit;
- **regulator:** at a pinned commit, with its Python dependencies through uv.

Installs SHALL run once per machine even when several slots start together. If a verification fails, the entrypoint SHALL exit non-zero without starting a worker.

#### Scenario: Fresh machine
- **WHEN** `subdomains` is activated on a machine without its tools
- **THEN** every tool and wordlist is installed and the worker starts polling `<fleet_id>.subdomains`

#### Scenario: Checksum mismatch
- **WHEN** a downloaded binary or wordlist does not match its pinned SHA-256
- **THEN** the slot's tab shows the mismatch and the exit code, and no worker polls from that slot

### Requirement: Fan-out across the fleet
The run SHALL enumerate each domain in its own child workflow, at most 100 at a time. Each child SHALL run the domain's steps as separate activities:
1. passive;
2. resolve;
3. brute force, then resolve again;
4. two permutation rounds;
5. regex permutations.

Any worker of the fleet's `subdomains` module MAY run any step, one step at a time per worker. Names SHALL pass between steps through Temporal. A permutation round's candidates SHALL be generated and resolved in one activity.

#### Scenario: Two machines share the work
- **WHEN** 4 domains are enumerated on a two-machine fleet with one worker per machine
- **THEN** 4 child workflows run, and both machines run their steps

### Requirement: Visible tool output
While a worker runs a step, the step's tool output SHALL stream into that worker's tab. The tab SHALL show:
- **first:** a line naming the run, the domain, the step and the attempt;
- **last:** a line with the step's count.

#### Scenario: Operator watches a chunk
- **WHEN** a domain's brute force runs on `wf-<fleet_id>-01`
- **THEN** the `subdomains #1` tab on that machine shows the run id, the domain, the step and puredns's live output

### Requirement: Chunk retries and timeout
- **Retries:** each step SHALL be retried on its own, on any worker, up to 3 attempts in total, when its tool fails or its worker is lost.
- **Durability:** completed steps SHALL NOT be run again.
- **Timeouts:**

  | step | timeout |
  | --- | --- |
  | passive | `passive_minutes` plus 15 minutes |
  | resolve | 4 hours |
  | brute force | 6 hours |
  | each permutation round | 6 hours |
  | regex permutations | 4 hours |

- **Lost workers:** a lost worker SHALL be detected within 2 minutes. A worker that loses its link to Temporal for less than about 100 seconds SHALL NOT lose its step.

#### Scenario: Worker lost mid-chunk
- **WHEN** the terminal running a domain's brute force is killed
- **THEN** within 2 minutes the brute-force step is retried on another worker, and the domain's passive and resolve steps are not run again

### Requirement: Run result
The run SHALL produce two files (see `run-files`):
- `subdomains.txt`: the resolved subdomains, lowercase, deduplicated and sorted;
- `passive.txt`: everything the passive sources reported.

Its result SHALL report:
- per domain, the counts of passive, resolved passive, brute-force, permutation, regex-permutation and total names, plus notes on skipped steps;
- the totals;
- each failed domain with its last error.

The run SHALL complete when at least one domain succeeded, and SHALL fail when every domain failed.

#### Scenario: Live enumeration
- **WHEN** `example.com` is enumerated on a `READY` fleet
- **THEN** the run completes with a count greater than 0, and every line of `subdomains.txt` is `example.com` or ends with `.example.com`

#### Scenario: Some chunks fail
- **WHEN** one of four domains fails after its retries
- **THEN** the run completes, reporting the three domains' counts and the failed domain with its error

### Requirement: Cancellation stops tools
Cancelling a run SHALL cancel its domain workflows and stop their in-progress tool processes (subfinder, puredns, massdns, gotator, regulator) and their children within one minute. The run SHALL end `CANCELED`.

#### Scenario: Cancel during enumeration
- **WHEN** a run is cancelled while two chunks are in progress
- **THEN** their tool processes have exited within a minute, and the workers' tabs show the chunks as cancelled

### Requirement: Input normalisation
The tool SHALL accept a list of root domains, 1 to 5,000 after normalisation. It SHALL trim, lowercase and deduplicate entries, and drop blank lines and lines starting with `#`. Entries that are not hostnames SHALL be rejected before a run starts, with up to ten of them named in the error.

#### Scenario: Messy input
- **WHEN** the tool is called with `[" Example.COM ", "example.com", "", "# note"]`
- **THEN** the run is started with the single domain `example.com`

#### Scenario: Invalid entry
- **WHEN** the input contains `https://example.com/path`
- **THEN** no run starts, and the error names that entry
