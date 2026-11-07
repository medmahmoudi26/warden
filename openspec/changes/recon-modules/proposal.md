# Proposal

## Why

The fleet only does passive subdomain enumeration with an unconfigured subfinder. That finds a fraction of what reconftw finds, and nothing after it: no live web discovery, no vulnerability scan, no content discovery. The operator now wants reconftw's subdomain logic (provider keys, DNS brute force, permutations) and three new recon steps on the fleet: an httpx probe, a nuclei scan and an ffuf scan with leaky-paths.

These steps produce far more than one Temporal payload can carry (2 MB). Their output has to reach the operator's disk as files, as reconftw writes them, rather than as an inline workflow result.

## What Changes

- **Subdomains, reconftw-style.** The `subdomains` module:
  - runs subfinder with the operator's `config/provider-config.yaml` and all sources;
  - resolves results with puredns (massdns, public resolvers plus trusted validation, wildcard filtering);
  - brute-forces each domain with reconftw's wordlist (n0kovo's huge list when `deep`);
  - generates two rounds of gotator permutations and regulator regex permutations, both resolved, with reconftw's size limits.

  `temporal_subdomain_enumeration` gains `bruteforce`, `permutations` and `deep` flags. **BREAKING:**
  - The result no longer lists subdomains inline. They are in the files `subdomains.txt` (resolved) and `passive.txt` (raw subfinder output).
  - The default run is active (DNS traffic to public resolvers) and much slower.
- **New `webs` module, `temporal_web_probe`.** httpx probes hosts or URLs and records status code, content length and type, title, server, technologies, CNAMEs, IPs, CDN, redirect location, favicon hash, body hash, response time and TLS data. Output: `webs.jsonl` (httpx JSON), `webs.txt` (one human-readable line per site) and `urls.txt` (live URLs).
- **New `nuclei` module, `temporal_nuclei_scan`.** nuclei runs all its default templates at every severity against the URLs. Output: `nuclei.jsonl` and reconftw-style human-readable `nuclei.txt`, plus one `nuclei-<severity>.txt` per severity found.
- **New `ffuf` module, `temporal_web_fuzzing`.** ffuf runs the leaky-paths wordlist (pinned) against each URL with reconftw's flags. Output: `ffuf.jsonl` and `ffuf.txt` lines of `<status> <length> <url>`, as reconftw writes them.
- **Run output files.** A module activity can return files. Warden collects them from the run's completed activities into `state/results/<run>/` when the run is fetched. This includes partial files for failed or cancelled runs. Every result tool lists the files with their paths, line counts and a preview.
- **Chaining.** The web probe, nuclei and ffuf tools accept `from_run`, a previous run whose output file becomes their input:
  - subdomains → webs;
  - webs → nuclei and ffuf.

  No host lists pass through the agent.
- **Fleet sizing and stability.**
  - `fleet_up` accepts a droplet `size` (nuclei wants more than 1 GB).
  - Fleet machines get a 2 GB swap file.
  - Run start waits up to 15 minutes for workers, since the heavier installs take longer, and fails at once if a slot already exited.
  - A run's execution timeout defaults to the fleet's remaining lifetime.
- **Self-contained modules, plain Temporal layout.** Each module folder carries:
  - `entrypoint.sh`, with only its own pinned installs;
  - `worker.py`;
  - `workflows.py`, workflow code only;
  - `activities.py`, activity code only;
  - `helpers.py`, every other function.

  No code is shared between modules.
- **Secrets.** `modules/*/config/provider-config.yaml` is git-ignored but still shipped to the fleet.

## Capabilities

### New Capabilities
- `run-files`: output files returned by module activities, collected into a local run folder, listed by result tools, and reusable as the input of another run (`from_run`).
- `module-runs`: what the web recon modules share: target normalisation, chunked fan-out, visible output, retries, timeouts, cancellation and pinned installs.
- `web-probing`: the `webs` module and `temporal_web_probe` (httpx with verbose fields, JSONL and human-readable output).
- `vulnerability-scanning`: the `nuclei` module and `temporal_nuclei_scan` (all default templates, JSONL and per-severity human-readable output).
- `content-discovery`: the `ffuf` module and `temporal_web_fuzzing` (leaky-paths wordlist, reconftw flags, JSONL and human-readable output).

### Modified Capabilities
- `subdomain-enumeration`: changes to pinned tools, provider config, DNS resolution, brute force, permutations, chunking defaults and the run result (now counts plus files).
- `mcp-tools`: changes to the tool listing (three new tools), `fleet_up` size, the worker wait and fail-fast at run start, the run lifetime default, and the run result shape (files).

## Impact

- **Code:**
  - `src/warden/runs.py` (file collection, `from_run` input, worker wait);
  - `src/warden/mcp.py` (three tools, target normalisation, `fleet_up` size);
  - `src/warden/fleet/models.py`, `src/warden/fleet/provider.py` (size, swap).
- **Modules:** new `modules/webs/`, `modules/nuclei/`, `modules/ffuf/`; a rewritten `modules/subdomains/`.
- **Fleet dependencies (installed by entrypoints, pinned):**
  - binaries checked against pinned SHA-256: subfinder 2.17.0, httpx 1.12.0, nuclei 3.11.1, ffuf 2.3.0, puredns 2.1.1;
  - from apt: massdns, golang-go and build-essential;
  - gotator, installed with `go install` at a pinned commit;
  - regulator, at a pinned commit, run through uv;
  - reconftw wordlists at a pinned commit, n0kovo's huge list and leaky-paths, each checked against a pinned SHA-256;
  - not pinned: trickest public resolvers (fetched fresh, they change daily) and nuclei templates (latest at install, version recorded in the result).
- **Network behaviour:** fleets now send large volumes of DNS queries and HTTP requests to targets. Use them only against authorised scope.
- **Tests:** module workflow tests on a Temporal dev server with fake tools; collection and chaining tests in `test_runs.py` and `test_mcp.py`; entrypoint install checks; an opt-in live end-to-end.
