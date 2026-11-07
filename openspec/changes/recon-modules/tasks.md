# Tasks

## 1. Control plane: run files, chaining, fleet sizing

- [x] 1.1 Git-ignore `modules/*/config/provider-config.yaml`; verify with `git check-ignore` and a `test_modules.py` test that the file is still in the module tarball
- [x] 1.2 Add `FleetSpec.size` (used by the provider instead of `WARDEN_DO_SIZE` when set) and a 2 GB swap file in cloud-init; verify with `test_provider.py` (size passed through, swap lines rendered)
- [x] 1.3 Collect `warden_files` from a run's completed activities into `state/results/<run>/`, merging per `run-files` (completion order, duplicate lines dropped, `.txt` sorted, atomic writes, name check); verify with `test_runs.py` on a dev server: multi-chunk merge, retried chunk counted once, bad names rejected
- [x] 1.4 Make `temporal_result` / `temporal_wait` return state, result (or `result.json` when large) and files with path, lines, bytes and a 10-line head for `.txt`, including partial files for failed/cancelled runs; verify with `test_runs.py` and `test_mcp.py`
- [x] 1.5 Add `from_run` input reading (`COMPLETED` only, source module check, assembles the folder if missing, empty file refused); verify with `test_runs.py`
- [x] 1.6 Wait up to 15 minutes for pollers and fail fast when a slot of the module has exited; default execution timeout to the fleet's remaining lifetime (max 24 h); verify with `test_runs.py`
- [x] 1.7 Add `size` to `fleet_up`, web target normalisation (hosts, `host:port`, http(s) URLs; 50,000 and 1.5 MB limits) in `mcp.py`; verify with `test_mcp.py`

## 2. Module layout

- [x] 2.2 Compress every payload between Warden and the fleet with one zlib codec (`src/warden/codec.py`, a copy in each module's `helpers.py`); verify with `tests/test_codec.py` and the 70,000-name passive test in `tests/modules/test_subdomains.py`
- [x] 2.1 Give every module the plain Temporal layout:
  - `entrypoint.sh` and `worker.py`;
  - `workflows.py`: workflow code only, with activities and helpers passed through the sandbox;
  - `activities.py`: activity code only;
  - `helpers.py`: the tool runner (process group, heartbeats, kill on cancel), file packing with the 1.5 MB guard, and every other function.

  Verify with `tests/modules/test_entrypoints.py` (every folder has the four files, valid entrypoints, checksum refusal) and the per-module tests.

## 3. subdomains: reconftw pipeline

- [x] 3.1 Rewrite `modules/subdomains/entrypoint.sh`:
  - pinned installs: subfinder 2.17.0, puredns, apt (massdns, golang-go, build-essential, git), gotator via `go install` at its commit, regulator venv;
  - pinned wordlists and trusted resolvers, plus fresh public resolvers;
  - a `--install-only` mode.

  Verify with `bash -n` and the checksum-mismatch test.
- [x] 3.2 Rewrite `modules/subdomains/workflows.py` and `activities.py`:
  - `SubdomainEnum` starts a `DomainEnum` child workflow per domain;
  - each step is its own activity: `passive`, `resolve`, `bruteforce`, `permute`, `regex_permute`;
  - reconftw's thresholds live in the workflow;
  - outputs are `subdomains.txt` and `passive.txt`.

  Verify with `tests/modules/test_subdomains.py` on a dev server with fake tools:
  - the step order in the child history;
  - a failed step retried alone, without redoing earlier steps;
  - the reconftw limits and the candidate cap;
  - failed domains reported;
  - cancellation kills the tools.
- [x] 3.3 Update `temporal_subdomain_enumeration` (flags `bruteforce`, `permutations`, `deep`; at most 5,000 domains) and its tests in `test_mcp.py`; Warden follows child workflows when collecting files, reports `pending_children`, and only touches top-level runs on teardown and deactivation (`test_runs.py`, `test_fleet_activities.py`)

## 4. webs module (httpx)

- [x] 4.1 Write `modules/webs/` (entrypoint with pinned httpx 1.12.0, worker, `WebProbe` workflow with verbose flags, `webs.jsonl` / `webs.txt` / `urls.txt`); verify with `tests/modules/test_webs.py` using a fake httpx
- [x] 4.2 Add `temporal_web_probe` (targets or `from_run` of a `subdomains` run, `ports`, `chunk_size=200`); verify with `test_mcp.py`

## 5. nuclei module

- [x] 5.1 Write `modules/nuclei/`:
  - an entrypoint with pinned nuclei 3.11.1 and the templates update;
  - a worker;
  - a `NucleiScan` workflow with default templates and every severity;
  - `nuclei.jsonl`, `nuclei.txt` and `nuclei-<severity>.txt`;
  - the raw-data fallback;
  - the templates version in the result.

  Verify with `tests/modules/test_nuclei.py` using a fake nuclei.
- [x] 5.2 Add `temporal_nuclei_scan` (targets or `from_run` of a `webs` run, `chunk_size=25`); verify with `test_mcp.py`

## 6. ffuf module

- [x] 6.1 Write `modules/ffuf/` (entrypoint with pinned ffuf 2.3.0 and leaky-paths at its commit, worker, `WebFuzz` workflow with reconftw flags, `ffuf.jsonl` / `ffuf.txt`); verify with `tests/modules/test_ffuf.py` using a fake ffuf
- [x] 6.2 Add `temporal_web_fuzzing` (targets or `from_run` of a `webs` run, `chunk_size=5`); verify with `test_mcp.py`

## 7. Documentation

- [x] 7.1 Update `modules/README.md` (file contract, shared library, data directory, provider config) and `README.md` (tool table, chaining, size and swap, active-scanning note); verify that the documented tool list matches `TOOL_NAMES`

## 8. Integration checks

- [x] 8.1 Install every entrypoint on Ubuntu 24.04 amd64 and confirm each tool reports its pinned version. Done on the live fleet's droplets rather than a container. The first installs found three bugs, all fixed:
  - massdns is not packaged for 24.04, so it is now built from its v1.1.0 commit;
  - `nuclei -duc` also blocked the template update, so the update now runs without it;
  - a wrong leaky-paths pin (truncated local copy) was corrected to the 40,242-byte file.

  The second install was clean: all 8 workers polling, puredns v2.1.1 with massdns, gotator, regulator, nuclei templates v10.5.1 and 12,123 public resolvers.
- [x] 8.2 Full test suite and ruff pass
- [x] 8.3 Live run on a fleet against a target the operator authorises:
  - subdomains → webs → nuclei and ffuf, chained with `from_run`;
  - files land in `state/results/`;
  - tabs show live output;
  - the fleet is torn down afterwards.

  Done on example.com with fleet `o7e4kt68` (2 × s-2vcpu-4gb). Every link of the chain ran through the MCP, and nothing was left after teardown. The run found and fixed:
  - **Payload limit:** 61,847 passive names exceeded it, so a zlib payload codec was added.
  - **`passive_minutes`:** subfinder's VirusTotal paging would use all 180 minutes, so the passive step is now bounded.
  - **Heartbeat slack:** 12 s lost a finished brute force, so heartbeats now go every 10 s with a 2-minute timeout, and a later blip was survived.
  - **`fleet_status`:** now reports hc reachability.

## Workflow follow-up

- Archive `warden-modules` before archiving this change (its specs are the base of this change's `subdomain-enumeration` and `mcp-tools` deltas).
- Restart `warden-serve` to load the new tools.
