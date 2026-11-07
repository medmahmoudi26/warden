# Tasks

## 1. Baseline and spikes

- [x] 1.1 Initialize git in `warden/` and commit the current tree, keeping `.env` ignored. Verify `git log --oneline` shows one commit and `git check-ignore .env` reports it as ignored.
- [x] 1.2 Pin the local herdr to v0.9.5 by installing the `herdr-macos-aarch64` asset from the v0.9.5 GitHub release, and get the operator's confirmation first. Verify `herdr --version` prints `0.9.5`. *Superseded:* Warden drives `hc` (Herdr.app), not terminal `herdr` (see 1.5); the upgrade is harmless.
- [x] 1.3 Spike: provision one throwaway droplet with tailscale and herdr v0.9.5. Use a hand-minted ephemeral `tag:warden-worker` key, and get operator approval first because it costs money. Verify that `herdr machine add root@<host>.tail1234.ts.net --label <name> --remote-session warden` runs without prompting. Verify that `herdr --machine <label>` works for `workspace create`, `tab create`, `pane rename`, `pane run`, `pane list` and `pane send-keys`. Also verify that `ssh root@<host> herdr pane list` works, and identify the remote version probe. Destroy the droplet afterwards and confirm through the DigitalOcean API that no droplet remains. *Superseded* for the herdr layer by 1.5. It still validated Tailscale SSH, the no-inbound firewall, and the presigned S3 path from a droplet.
- [x] 1.4 Save the spike's JSON outputs as fixtures under `tests/fixtures/herdr/`. Record the chosen default transport and the version-probe command in design.md D3. Verify the fixtures exist and D3 names the default. *Superseded* by 1.6.
- [x] 1.5 `hc` spike on one throwaway droplet, with operator-approved spend. The droplet gets tailscale through cloud-init, and no `hc`. Verify:
  - `hc machine add root@<host>.tail1234.ts.net --name wf-<id>-01 --temp --install --json` runs unattended (stdin closed);
  - `hc machine ls --json` shows the machine connected, with the local `hc` version;
  - `hc run --on <m> --bg --no-parent --name <job> --label warden.job=<job> -- <argv>` starts a terminal that the operator confirms seeing live in Herdr.app;
  - `hc get`, `hc ls --all --label warden.job=<job>`, `hc interrupt`, `hc rm` and `hc exec --on <m> --json -- <argv>` (returns rc and output) all work;
  - `hc machine rm <m>` removes the profile.

  Destroy the droplet afterwards, and confirm through the APIs that no droplet, tailnet device or `hc` machine remains.
- [x] 1.6 Save the `hc` spike's JSON outputs as fixtures under `tests/fixtures/hc/` and delete `tests/fixtures/herdr/`. Record any deviation from the D3 operations table in design.md D3. Verify the fixtures contain no presigned URLs or keys, and that D3 matches the observed commands.

## 2. Control-plane store and configuration

- [x] 2.1 Update `pyproject.toml`:
  - add `pulumi-tailscale`, `boto3` and `duckdb`;
  - remove `pulumi-docker`, `pyarrow` and the `warden-worker` script.

  Verify `pip install -e ".[dev]"` succeeds in `.venv`.
- [x] 2.2 Add `warden/config.py`: typed settings loaded from the environment, covering tailnet, suffix, control host, S3 keys and endpoints, herdr version, DigitalOcean settings and lifetimes. Update `.env.example` and drop the Flight, Docker and worker keys. Verify with a unit test that a missing required key raises an error naming that key.
- [x] 2.3 Add `deploy/compose.yaml` for SeaweedFS on `127.0.0.1:8333`, with data in `state/seaweedfs`. Add a generator for `deploy/seaweedfs-s3.json` that reads the S3 keys from the environment. Verify `docker compose -f deploy/compose.yaml up -d`, then confirm `aws --endpoint-url http://127.0.0.1:8333 s3 ls` authenticates.
- [x] 2.4 Publish the store on the tailnet with `tailscale serve --bg --tcp 8333 tcp://localhost:8333` (via the app CLI path). Verify `tailscale serve status` lists 8333. Also verify that `curl` to the MacBook's LAN IP on port 8333 is refused, as the result-store spec requires.
- [x] 2.5 Add `warden/store.py` (design D9):
  - local and public `boto3` clients;
  - `ensure_bucket`;
  - key builders for `runs/<id>/...` with per-attempt shard keys, and for `fleets/<id>/probe/<machine>`;
  - presigning with expiry clamped to [timeout + 15 min, 24 h].

  Verify with unit tests for key layout and expiry clamping.
- [x] 2.6 Add an opt-in store integration test (`WARDEN_STORE_IT=1`) covering:
  - a presigned PUT and then GET through the public tailnet endpoint;
  - a URL rejected when used for another key;
  - an expired URL rejected.

  Verify it passes against the running SeaweedFS.
- [x] 2.7 Write the README's "Operator setup" section: herdr pin, the SSH stanza from design D3, `.env` keys, compose, and `tailscale serve`. Verify each documented command runs as written on the MacBook.

## 3. Module catalog, `warden-run` and `hc` driver

- [x] 3.1 Rework `catalog.py` into command templates, each with `{in}`, `{out}` and `timeout_s`, and set subfinder to `subfinder -dL {in} -silent -o {out}` with a 1800 s timeout. Verify with unit tests: subfinder is listed, a template missing a placeholder is rejected, and an unknown module error lists the known names.
- [x] 3.2 Add `src/warden/assets/warden-run.sh` following design D6: fetch, substitute, `timeout` with a live log, `INT` trap, uploads with the marker last, and exit with `rc`. Verify with a pytest that runs it against a local fake HTTP PUT/GET server and covers `rc=0`, `rc!=0`, timeout (`reason=timeout`) and `INT` (`reason=cancelled`). Skip the test when GNU `timeout` is unavailable.
- [x] 3.3 Add `work/hc.py` (`HcDriver`) per design D3:
  - the operations table;
  - `--json` parsing;
  - typed `HcError`;
  - a missing terminal or machine reported as `None`, or as success on remove;
  - the ownership guard: mutating calls require `wf-*` machine names and the `warden.job` label.

  Verify with unit tests that mock `subprocess`, replay `tests/fixtures/hc/`, and check that the guard rejects other machines and unlabelled terminals before `hc` is invoked.
- [x] 3.4 Add registration helpers:
  - `register_machine` (`hc machine add --install`, without `--temp` so it is listed in Herdr.app);
  - `unregister_machine` (`hc machine rm`, where missing counts as success);
  - `prune_known_hosts` (`ssh-keygen -R -f ~/.ssh/known_hosts.warden`).

  Verify with mocked-`subprocess` unit tests, including refusing a non-`wf-*` name.
- [x] 3.5 Switch the configuration from terminal herdr to `hc`:
  - in `config.py`, drop the `herdr_*` settings and add `hc_bin` (default `hc`);
  - remove the `WARDEN_HERDR_*` keys from `.env.example` and `.env`;
  - rewrite README "Operator setup" step 2 for Herdr.app and `hc`.

  Verify `tests/test_config.py` passes and the README's `hc` commands (`hc --version`, `hc server status`) run as written.
- [x] 3.6 Add `HerdrApp` to `work/hc.py` per design D3. It wraps the documented `herdr-app` CLI:
  - `ensure_space(machine)` creates or reuses the `warden <machine>` space on that machine with `space.new --machine`;
  - `close_spaces_on(machine)` closes that machine's spaces;
  - only `wf-*` machines are accepted.

  Job terminals opt into the window with the `herdr.space` label. This replaces the earlier layout-document approach, which Herdr.app overwrites. Verify with unit tests against a fake `herdr-app`: a space is created once and reused, closing touches only that machine's spaces, errors surface, and non-fleet machines are refused before any call.
## 4. Job execution

- [x] 4.1 Add the `JobSpec` and `JobResult` models and the job id format `<run_id>-<shard_id>-a<n>` (at most 32 characters, as `hc` requires) to `models.py`. Verify with a unit test of the id format and length, and of round-trip serialization through the Temporal data converter.
- [x] 4.2 Add input staging: normalize (trim, drop blank lines, dedupe with order kept) and write `runs/<id>/input.txt`, taking either inline targets or a previous run's `result.parquet`. Verify with unit tests for the recon-runs "Inline targets" and "Chained input" scenarios.
- [x] 4.3 Rewrite the `plan_shards` activity: read `input.txt` from the store, split it into contiguous shards (no more shards than lines; default one shard per machine; `machine_index = i mod N`), and write `shards/<id>.in`. Verify unit tests for 10 lines over 3 machines (4, 3, 3), 2 lines over 3 machines (2 shards), and empty input (no shards).
- [x] 4.4 Implement the `run_job` activity per design D7 (marker check, `hc get` lookup, launch with labels, wait, lost, cancel, heartbeats). Verify with unit tests using a fake driver and fake store:
  - an existing marker means nothing is started;
  - a live terminal means nothing is started, then the marker is awaited;
  - a finished terminal with no marker raises `JobLost` after the grace period;
  - an absent terminal leads to `hc run` with the four `warden.*` labels;
  - cancellation calls `hc interrupt`;
  - `rc!=0` returns a failed result without raising.
- [x] 4.5 Implement `merge_results`, `read_results` and `query_results` (DuckDB) per design D10. Verify with unit tests:
  - duplicates are removed across shards;
  - only successful attempts are merged;
  - the page limit is capped at 10,000 and `total_rows` is returned;
  - an aggregate SQL query works;
  - a local file read, `COPY TO` and an `httpfs` URL are all rejected.

## 5. Fleet lifecycle

- [x] 5.1 Rework `providers/digitalocean.py` per design D4/D5:
  - `TailnetKey` (ephemeral, preauthorized, reusable, 1 h, `tag:warden-worker`);
  - droplets tagged `warden` and `fleet:<id>`;
  - a firewall with no inbound rules;
  - cloud-init with tailscale and sha256-verified subfinder only; `hc` and `warden-run` are installed by the readiness gate;
  - `tailnet_host` in the inventory; `exec_mode` and SSH logic removed.

  Verify with Pulumi-mock unit tests (resource counts and properties) and with cloud-init rendering tests (`tailscale up` flags, hostname, tag, subfinder sha check, no `hc` or `warden-run`).
- [x] 5.2 Make provisioning fail fast with the non-retryable `MissingCredentialsError` when the DigitalOcean token or `TAILSCALE_OAUTH_CLIENT_*` is missing. Verify with a unit test that the error names the missing setting and the provider's `up` is never called.
- [x] 5.3 Add `warden/tailscale_api.py`: OAuth token exchange, devices listed by tag and hostname prefix, and device delete. Verify with unit tests against a mocked HTTP transport, and with an opt-in live check that the token exchange succeeds with `.env`.
- [x] 5.4 Implement the `wait_fleet_ready` activity per design D8:
  1. device online;
  2. `hc machine add --install`, retried until SSH answers;
  3. `hc machine ls` shows `connected`; the version is recorded, and a difference from local `hc` is logged rather than fatal;
  4. `hc exec` `cloud-init status` reports done (stdout is base64);
  5. `hc file write` installs the control plane's `warden-run`;
  6. `hc exec` curl PUT to a presigned `fleets/<id>/probe/<machine>`, then HEAD it.

  It sends heartbeats, and errors name the machine. Verify with fake-backed unit tests for success, version difference (still ready), probe failure and timeout.
- [x] 5.5 Implement the teardown activities, all idempotent: `unregister_machines` (`HcLayout.remove_spaces_for_fleet`, then `hc machine rm`), `destroy_fleet` (Pulumi destroy), `delete_tailnet_devices` and `prune_known_hosts`. Verify with unit tests that a second destroy of the same fleet succeeds and changes nothing.
- [x] 5.6 Implement `FleetWorkflow` per design D8, replacing `FleetOrchestratorWorkflow` and `FleetExecutorWorkflow`. It needs:
  - the states `PROVISIONING`, `READY`, `DESTROYING`, `DONE` and `FAILED`;
  - the deadline timer;
  - the `request_destroy` signal;
  - destroy-on-cancel;
  - `fleet_ready` / `fleet_failed` signals to its parent;
  - a status query that includes the deadline.

  Verify with time-skipping workflow tests:
  - ready, then the destroy signal leads to `DONE`;
  - the deadline elapsing destroys the fleet and records the reason;
  - a cancel runs the destroy activities;
  - a readiness failure ends `FAILED` with destroy run;
  - a duplicate start is a no-op.
- [x] 5.7 Implement `SweepWorkflow` and its activity per design D13: DigitalOcean droplets tagged `warden`, tailnet devices tagged `tag:warden-worker` and `hc` machines named `wf-*`, compared against running `FleetWorkflow`s. Verify with fake-backed tests that an orphan is destroyed and unregistered, a running fleet is left alone, and non-`wf-*` `hc` machines are never touched.

## 6. Recon runs

- [x] 6.1 Implement `ReconRunWorkflow` per design D8:
  - scoped child fleet (`REQUEST_CANCEL`) or an existing `READY` fleet;
  - `plan_shards`, then one coroutine per shard with a per-machine lock and an attempt loop calling `run_job`;
  - `merge_results`, terminal cleanup, and scoped fleet release;
  - a status query.

  Verify with time-skipping tests (fake activities):
  - the placement for 10 lines over 3 machines;
  - partial success ends `DONE` with the failed shard listed;
  - all shards failing ends `FAILED`;
  - empty input ends `DONE` with no `run_job` calls;
  - an attempt 2 success;
  - a fleet that is not `READY` is rejected;
  - a scoped fleet is destroyed on finish and on cancel.
- [x] 6.2 Retention: job terminals are not cleaned up per run. Every job's terminal and tab stays until its fleet is destroyed: `warden-run --hold` keeps the terminal open after the marker, and teardown closes the machines' Herdr spaces. Verify with unit tests: `run_job` launches with `--hold` and the `herdr.space` label, still runs when Herdr is unavailable, and teardown and sweep close each machine's spaces.
## 7. Client API, MCP, serve, and removing legacy code

- [x] 7.1 Rewrite `client_api.py`:
  - fleet handle: start, status, destroy, wait;
  - `run_module`, which stages input and then starts `ReconRunWorkflow` (`run/<run_id>`);
  - run handle: status, wait, cancel.

  Verify with unit tests on a time-skipping environment.
- [x] 7.2 Rewrite the MCP tools per design D11: `list_modules`, `start_fleet`, `fleet_status`, `destroy_fleet`, `run_module`, `run_status`, `wait_run`, `cancel_run`, `read_results` and `query_results`. Remove the old tools. Verify with unit tests that call each tool with fakes, and that `fastmcp` lists exactly these tools.
- [x] 7.3 Rewrite `serve.py`:
  - drop Flight;
  - register the new workflows and activities on `warden-orchestrator-queue`;
  - startup checks (Temporal, S3 bucket, `hc` on PATH and `hc server status`, Tailscale token) that fail fast with a clear message;
  - start the sweep.

  Verify with unit tests for each failing-check message, and that `warden-serve --help` runs.
- [x] 7.4 Delete the legacy code:
  - `flight_server.py`, `flight_client.py`, `flight_store.py`;
  - `work/ssh_exec.py`, `worker.py`, `Dockerfile.worker`;
  - `providers/docker.py`, the `activities.py` shim and the old workflow modules;
  - the Flight, Docker and old plan-shard tests;
  - the `models.py` leftovers (`ShardTicket`, `ExecutorState`, `exec_mode`, `dataset_path`).

  Verify that `grep -rE "flight|ssh_exec|exec_mode|docker" src tests` finds no live references and that `PYTHONPATH=src pytest -q` passes.
- [x] 7.5 Rewrite the README (architecture, MCP tools, operator setup) and update or remove `scripts/drive_do_enum.py` to match the new client API. Verify that the README's MCP flow matches the tool list from 7.2 and that the script runs with `--help`.

## 8. End-to-end on DigitalOcean (opt-in, operator-approved spend)

- [ ] 8.1 *Superseded by `warden-modules` (its task 7.1 runs the e2e on the new design); not run here.* Add an opt-in e2e test (`WARDEN_E2E=1`) that:
  1. runs `run_module("subfinder", targets=["everydaylabs.com"], instances=1)`;
  2. waits for `DONE` with `rows > 0`;
  3. checks that `read_results` and `query_results` return data;
  4. after teardown, finds no droplet tagged `fleet:<id>` (DigitalOcean API), no tailnet device with that prefix (Tailscale API), and no `hc` machine `wf-<id>-*`.

  Verify it passes, and record whether the operator saw the job terminal live in Herdr.app.
- [x] 8.2 During an e2e run, probe the droplet's public IPv4 on ports 22, 80 and 8333 from the MacBook. Verify every probe is refused or times out, as the fleet-lifecycle spec's "No public inbound exposure" requires.
- [ ] 8.3 *Superseded by `warden-modules`: jobs no longer resume through hc and S3 markers; Temporal activity retries replace them. Not run.* Resume check: stop `warden-serve` mid-job, restart it, and let the run finish. Verify the run reaches `DONE` and `hc ls --all --label warden.job=<job>` shows exactly one terminal for the job (no duplicate process).
- [x] 8.4 Lifetime check: `start_fleet(instances=1, max_lifetime_minutes=5)`, then wait. Verify the fleet result reports destruction by the lifetime limit and the DigitalOcean API shows no droplet for it.
- [ ] 8.5 *Superseded by `warden-modules`, which keeps the sweep unchanged; this live check was not run.* Sweep check: start a fleet, run `temporal workflow terminate` on its `FleetWorkflow`, then restart `warden-serve`. Verify the sweep log names the fleet and that no droplet, tailnet device or `hc` machine for it remains.

## Workflow follow-up

- Rotate the Tailscale OAuth client to a credential scoped only to `auth_keys` and `devices:core` (tag `tag:warden-worker`), and update `.env`.
- Archive this change once e2e passes and the specs have been reviewed.
- Candidate follow-up changes: a Packer-prebaked image, agent swarms on the `hc` driver, an always-on control host.
