# Tasks

## 1. Prerequisites and spikes

- [x] 1.1 Close `herdr-fleet-tailscale`, with the operator's confirmation:
  - mark its tasks 8.1, 8.3 and 8.5 as superseded by `warden-modules`;
  - archive it.

  Verify:
  - `openspec list --specs` shows `fleet-lifecycle`, `job-execution`, `recon-runs` and `result-store`;
  - `openspec validate warden-modules --strict` no longer reports that the `fleet-lifecycle` delta targets a missing spec.
- [x] 1.2 Spike the `hc` details the design relies on (design D3, D4):
  - write a gzip tarball with `hc file write` (locally, and `--on` a fleet machine if one is up);
  - `hc kill` a live terminal;
  - `hc run --on <m> --cwd <dir>`.

  Save the `kill` JSON as `tests/fixtures/hc/kill.json`.

  Verify:
  - the written file's SHA-256 matches the source (otherwise switch D3 to the base64 fallback and note it in design.md);
  - `kill` ends the terminal's program;
  - `--cwd` is honoured on the remote machine.
- [x] 1.3 Operator setup for Temporal on the tailnet (design D2):
  - run `tailscale serve tcp:7233 tcp://127.0.0.1:7233`;
  - in the tailnet policy, replace the `warden-control:8333` grant with `:7233`;
  - update the policy tests (workers reach 7233; cannot reach 22, 8233, 8333).

  Verify:
  - `tailscale serve status` lists 7233;
  - `temporal workflow list --address <control-host>:7233` works over the tailnet name;
  - a connection to the MacBook's LAN IP on 7233 is refused.
- [x] 1.4 Retire SeaweedFS: run `docker compose -f deploy/compose.yaml down` and remove the `tailscale serve` entry for 8333. Verify `tailscale serve status` no longer lists 8333 and nothing listens on `127.0.0.1:8333`.

## 2. Remove the work layer and restructure

- [x] 2.1 Delete the work layer:
  - **code:** `src/warden/work/{jobs,shards,results,recon}.py`, `src/warden/assets/`, `catalog.py`, `store.py`, `client_api.py`, and the run and job models in `models.py`;
  - **deploy:** `deploy/`;
  - **tests:** `test_store*`, `test_catalog`, `test_job_models`, `test_shards`, `test_results`, `test_jobs`, `test_warden_run`, `test_api`;
  - **dependencies:** `boto3` and `duckdb` from `pyproject.toml`;
  - **settings:** `WARDEN_S3_*` from `config.py` and `.env.example`;
  - **readiness:** the `install` and `probe` stages.

  Verify `grep -rnE "boto3|duckdb|presign|warden-run|catalog|Store\(" src tests` finds nothing and `PYTHONPATH=src pytest -q` passes.
- [x] 2.2 Restructure into the layout of design D10:
  - `work/hc.py` becomes `hc.py`;
  - `tailscale_api.py` becomes `tailscale.py`;
  - `providers/` + `infra/` + the fleet models become `fleet/{models,provider,state,activities,workflow}.py`.

  Update imports and tests. Verify `pytest -q` passes and `src/warden/` contains no `work/`, `infra/` or `providers/` folders.
- [x] 2.3 Replace `dependencies.py` with constructor injection (design D9). Fleet activities become methods of `FleetActivities(settings, hc, herdr, tailscale, client, provider)`, and tests build it with `tests/fakes.py`. Verify `grep -rn "dependencies" src tests` finds nothing and `pytest -q` passes.

## 3. Fleet lifecycle

- [x] 3.1 Add the settings `modules_dir` (`WARDEN_MODULES_DIR`, default `modules`) and `temporal_fleet_address` (`WARDEN_TEMPORAL_FLEET_ADDRESS`, default `<WARDEN_CONTROL_HOST>:7233`), and document both in `.env.example`. Verify with unit tests for the defaults and overrides.
- [x] 3.2 In cloud-init, replace the subfinder install with a pinned `uv` release, verified by SHA-256 from its GitHub release (design D10). Verify with a unit test of `render_cloud_init` that it installs and checks uv, and that it no longer mentions subfinder.
- [x] 3.3 Update the readiness stages in this order (design D8):
  1. tailnet;
  2. register;
  3. connected;
  4. boot;
  5. space (best effort, via `HerdrApp.ensure_space`);
  6. Temporal probe (`hc exec ... </dev/tcp/<host>/7233`).

  Verify with unit tests on the fake `hc`:
  - a failing probe fails readiness with the machine and address named;
  - a failing Herdr call doesn't block `READY`.
- [x] 3.4 Extend `hc.py`:
  - add `kill(addr)`;
  - replace the job guard with the module-terminal guard (name `^[a-z][a-z0-9-]{0,23}-\d{1,2}$`, label `warden.module`, `wf-*` machine);
  - allow `--cwd` on `run`.

  Verify with unit tests using fixtures. The guard must reject unlabelled terminals and non-fleet machines before `hc` is invoked.
- [x] 3.5 Add `terminate_fleet_runs(fleet_id)` to `FleetActivities` (design D7): it lists running workflows, keeps ids starting with `run/<fleet_id>/`, and terminates them with reason `fleet <id> destroyed`. Verify with a time-skipping test: only that fleet's runs are terminated, and the reason is recorded.
- [x] 3.6 Simplify `FleetWorkflow` (design D8):
  - remove parent signalling and run-scoped handling;
  - add `FleetSpec.modules` and an `activate_modules` step after `READY`, with errors in `FleetStatus.module_errors`;
  - make teardown start with `terminate_fleet_runs`.

  Verify with time-skipping tests:
  - an activation error leaves the fleet `READY` with the error reported;
  - destroy terminates an in-flight run before machines are destroyed;
  - repeated destroy is a no-op.
- [x] 3.7 Add `fleet/client.py`:
  - `start` (duplicate start is a no-op);
  - `list` (running `FleetWorkflow`s plus each one's status query);
  - `status`;
  - `destroy`.

  Verify with a time-skipping test: two fleets are listed, and destroying one leaves the other `READY`.
- [x] 3.8 Update the README's architecture, operator setup and fleet sections:
  - Temporal is published on 7233 with the `127.0.0.1` target;
  - the policy grant;
  - no SeaweedFS.

  Verify each documented operator command runs as written on the MacBook.

## 4. Modules

- [x] 4.1 Add discovery to `modules.py`:
  - folders matching `^[a-z][a-z0-9-]{0,23}$` with an executable `entrypoint.sh`;
  - names starting with `_` or `.` are skipped;
  - the description is the first comment line.

  Verify with unit tests on temporary folders: listing, a missing entrypoint, a skipped `_lib`, and the unknown-module error listing the available names.
- [x] 4.2 Add `pack(module)`, which builds an in-memory gzip tarball excluding `.venv`, `__pycache__`, `.git` and `*.pyc`. Verify with a unit test that the excluded paths are absent and everything else round-trips.
- [x] 4.3 Add `activate(fleet, module, workers)` (design D3, D4):
  - check that the fleet is `READY` and the module is known;
  - per machine, in parallel: `ensure_space` (best effort); ship only when no slot is live; launch the missing slots with the exact `hc run` argv, environment and labels of D4; rename tabs to `<module> #<n>`;
  - return per-machine outcomes.

  Verify with unit tests on the fake `hc`:
  - the argv and environment match D4;
  - a second activation starts nothing;
  - scaling from 1 to 3 workers starts only slots 2 and 3;
  - a failing machine is reported while the others succeed;
  - without a Herdr window, the slots still launch without the space label.
- [x] 4.4 Add `module_state(fleet)` (design D5), built from `hc ls --label warden.fleet=<id>` plus one `hc exec` per machine reading `/var/lib/warden/slots/*.rc`. Verify with unit tests that slots are reported as running, exited with an rc, or absent.
- [x] 4.5 Add `deactivate(fleet, module, force)`:
  - refuse when `run/<fleet>/<module>-` runs are in flight, unless forced (forcing cancels them first);
  - otherwise interrupt, wait up to 30 s for rc files, kill, then remove.

  Verify with unit tests: the refusal names the run; force cancels, then stops; an unlabelled terminal in the same space is never touched.
- [x] 4.6 Wire the `activate_modules` activity in `FleetActivities` to `modules.activate` with one worker each. Verify with a unit test that per-module errors are returned rather than raised.
- [x] 4.7 Write `modules/README.md` covering the module contract:
  - `entrypoint.sh` and its first-line description;
  - the environment variables;
  - Temporal workers setting `identity=$WARDEN_WORKER_ID`;
  - exit behaviour;
  - the rule that code is shipped only when no slot is live.

  Verify it matches `fleet-modules` and design D4 and D12.

## 5. Runs and MCP tools

- [x] 5.1 Add the start path to `runs.py`:
  - `resolve_fleet` (the given fleet, or the only `READY` one, or an error listing the candidates);
  - `ensure_active`;
  - `wait_pollers` (`describe_task_queue` identities for both task types, 2 s interval, 300 s limit);
  - `start_run` (id `run/<fleet>/<module>-<hex6>`, queue `<fleet>.<module>`, timeout capped at the fleet deadline).

  Verify with time-skipping tests using a local worker with the expected identity: the start succeeds; a missing poller fails naming the slot; the timeout is capped by the deadline.
- [x] 5.2 Add `status`, `result` (above 20,000 characters, write to `state/results/<fleet>-<module>-<hex6>.json` and return the path, size and preview), `wait` and `cancel` to `runs.py`. All of them refuse ids that don't start with `run/`. Verify with tests for:
  - a running status with pending activities;
  - inline and file results;
  - wait timing out to status;
  - cancelling a finished run being a no-op;
  - `fleet/...` being refused.
- [x] 5.3 Write `mcp.py` with the 12 hand-written tools (`mcp-tools` spec) and delete `mcp_server.py`. `temporal_subdomain_enumeration` normalises and validates its input (trim, lowercase, deduplicate, drop blanks and `#` lines, hostname check, 1–10,000 domains) before calling `start_run(..., "subdomains", "SubdomainEnum", ...)`. Verify with tests:
  - the tool list equals the spec's 12 names;
  - messy input becomes `["example.com"]`;
  - `https://example.com/path` is rejected with no run started;
  - more than 10,000 domains is rejected.
- [x] 5.4 Update `serve.py`:
  - startup checks: Temporal, `hc`, Tailscale, plus a warning if the modules directory is missing;
  - build `FleetActivities`;
  - register the control-plane workflows and the MCP.

  Verify that `warden-serve --transport http --port 8008 --no-sweep` starts against the dev server, and that `claude mcp list` shows warden connected with the 12 tools.
- [x] 5.5 Update the README's MCP section: the tools by prefix, the raw `hc` path with the space label from `fleet_status`, and "adding a capability" (a module folder plus one tool function). Verify the documented tool names match `mcp.TOOLS`.

## 6. The `subdomains` module

- [x] 6.1 Write `modules/subdomains/entrypoint.sh` (design D11):
  - a description comment line;
  - install subfinder `v2.6.7` with SHA-256 verification unless it is already installed;
  - then `exec uv run --script worker.py`.

  Verify with `bash -n`, and with a test that runs the install step against a fake download: a mismatched checksum exits non-zero and nothing is installed.
- [x] 6.2 Write `modules/subdomains/worker.py`:
  - a PEP 723 header (`temporalio>=1.35`);
  - connect to `TEMPORAL_ADDRESS` with `identity=WARDEN_WORKER_ID`;
  - poll `WARDEN_TASK_QUEUE` with `max_concurrent_activities=1`;
  - shut down gracefully on SIGINT or SIGTERM.

  Verify by running it locally with `uv run --script` against the dev server on a test queue: `temporal task-queue describe` shows the identity, and ctrl+c exits within 10 s.
- [x] 6.3 Write `modules/subdomains/workflows.py`, with `SubdomainEnum` and `enumerate_chunk` per design D11. Verify with time-skipping tests in `tests/modules/test_subdomains.py`, using a fake `subfinder` script on `PATH`:
  - 100 domains make 4 chunks;
  - output is deduplicated and sorted;
  - one failing chunk gives a partial result with that chunk's domains and error;
  - an all-failing run fails;
  - a failed attempt is retried once;
  - cancellation kills the fake process group;
  - the 1.5 MB guard fails clearly.

## 7. End-to-end on DigitalOcean (opt-in, operator-approved spend, MCP tools only)

- [x] 7.1 Rewrite `tests/test_e2e.py` (`WARDEN_E2E=1`) to:
  1. call `fleet_up(instances=2, modules=["subdomains"])` and wait for `READY`;
  2. check `fleet_status` shows `subdomains` slot 1 running on both machines;
  3. call `temporal_subdomain_enumeration(["example.com"])` and `temporal_wait`, and check `count > 0` with every subdomain under `example.com`;
  4. call `fleet_down`;
  5. confirm that no droplet, tailnet device, `hc` machine or Herdr space remains for the fleet.

  Verify it passes, and record whether the operator saw the `subdomains #1` tabs and the subfinder output live in Herdr.app.

  *Result:* passed in 4 min 21 s (fleet `kp9hfhzy`). The operator saw the `subdomains #1` tab with subfinder's live output on fleet `i6ipzkv2`.
- [x] 7.2 Raw path: on a live fleet, run `hc run --on wf-<id>-01 --bg --label herdr.space=<label from fleet_status> -- uname -a`. Verify the operator sees it as a new tab in that machine's Warden space, and that `fleet_down` removes it.

  *Result:* on `i6ipzkv2` the operator saw tab `3` (`cwd: /opt/warden/modules/subdomains`, uname) beside `subdomains #1`. On `8qvmqa8k`, `fleet_down` closed the space and left no Herdr space for the fleet.
- [x] 7.3 Lost worker and cancellation:
  - **Lost worker:** start an enumeration on enough domains to take several minutes, then `hc kill` the slot running a chunk. Verify the chunk is retried on the other machine and the run completes.
  - **Cancellation:** start another run and call `temporal_cancel`. Verify the run ends `CANCELED` and `hc exec --on <m> -- pgrep subfinder` finds nothing on either machine within a minute.
- [x] 7.4 Two fleets: start a second fleet, then call `fleet_down` on the first while a run is in flight on it. Verify:
  - `fleet_list` showed both fleets;
  - the run ends `TERMINATED` with the fleet-destroyed reason;
  - the second fleet stays `READY` with its module running.
- [x] 7.5 Exposure: during a run, probe a droplet's public IPv4 on ports 22, 80 and 7233 from the MacBook. Verify every probe is refused or times out.

## Workflow follow-up

- Archive `warden-modules` once e2e passes and the specs have been reviewed.
- Candidate follow-up changes:
  - Temporal mTLS with a per-fleet namespace;
  - a Packer image with `uv` and common tools prebaked;
  - a claim-check payload codec for large module outputs;
  - an agent-swarm module;
  - rotating the Tailscale OAuth client to `auth_keys` + `devices:core`.
