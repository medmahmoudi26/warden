# Design

## Context

Motivation and scope are in `proposal.md`, and requirements are in `specs/`. The constraints that shape the approach are below.

**Control plane: the operator's MacBook**

| Item | Value |
|---|---|
| Tailnet hostname | `my-laptop.tail1234.ts.net` |
| Tailnet IP | `100.64.0.1` |
| Temporal dev server | `127.0.0.1:7233`, persisted to `.temporal/dev.db`. Pinned as `TEMPORAL_HOST=127.0.0.1:7233`: `localhost` resolved to IPv6 `[::1]:7233`, a separate Docker Desktop Temporal extension that earlier Warden versions used by accident |
| Herdr | `Herdr.app` (0.1.13) running on `hc` (herdr-core) 0.1.18 after an app update during the spike; at `~/.local/bin/hc` |
| Other local tools | Docker Desktop, `duckdb`, `aws` |

The operator's `hc` server already hosts their own terminals (12 live during planning) and an unrelated machine.

**Tailnet policy (already applied)**
- It defines `tag:warden-worker` and `hosts.warden-control = 100.64.0.1`.
- Workers may reach only `warden-control:8333`.
- Members may Tailscale-SSH into workers as `root` with `accept`.
- Tests on every save assert that workers cannot reach ports 22, 7233 or 8233 on the control host.

**Tailscale credentials:** OAuth client credentials are in `.env` (`TAILSCALE_OAUTH_CLIENT_ID` and `TAILSCALE_OAUTH_CLIENT_SECRET`). The client currently has scope `all`.

**`hc` facts the approach depends on** (from `hc --help` 0.1.17):
- Machines are reached over SSH and registered with `hc machine add <target> [--name N] [--temp] [--install]`:
  - `--install` copies `hc` to the machine without asking;
  - `--temp` marks the machine short-lived, so clients show it apart from permanent machines.
- `hc machine rm <name>` forgets a machine. Its terminals keep running there.
- `hc run --on <machine> --bg --name <n> --label k=v ... -- <argv>` starts a program in a named, labelled terminal and returns at once. The terminal's address is `<machine>/<name>`, and the program is given as an argv, not typed into a shell.
- `hc get <addr> --json` and `hc ls [--all] [--label k=v] --json` report terminal state.
- `hc interrupt <addr>` presses the stop key.
- `hc rm <addr>` removes a finished terminal.
- `hc exec --on <machine> -- <argv>` runs a program without a terminal and returns exactly its output and exit code (timeout at most 600 s).
- `hc machine ls --json` reports each machine's `state` and remote `version`.

**Terminal `herdr` was considered and dropped.** The first spike (tasks 1.2–1.4) targeted the separate terminal `herdr` 0.9.5. Its machine registry is not read by Herdr.app, so fleet work would never appear in the operator's UI. The spike still validated, on a real droplet:
- Tailscale SSH;
- the no-inbound firewall;
- the S3 data path from droplet to `tailscale serve` to SeaweedFS, with presigned PUTs.

Its fixtures are superseded by the `hc` spike.

**What exists today:**
- Fleet lifecycle (`infra/workflows.py`: `FleetOrchestratorWorkflow` → `FleetExecutorWorkflow`).
- A Pulumi DigitalOcean program (`providers/digitalocean.py`) with a local file backend under `state/pulumi/`.
- An MCP server (`mcp_server.py`) and a client API (`client_api.py`).
- The data plane (Arrow Flight), SSH exec and the Docker provider, all of which are removed.

## Goals / Non-Goals

**Goals:**
- Large data never passes through Temporal history: workflows carry object keys and counts only.
- Every control-plane activity is safe to retry or resume. A restart of `warden-serve` or of Temporal never duplicates a job or leaks a droplet.
- One driver abstraction over `hc`, so workflows never shell out directly. Agent swarms will reuse it later, since `hc` also manages coding agents.
- Warden only ever touches `hc` machines and terminals it created. The operator's own terminals and machines are never listed for mutation, interrupted or removed.
- The first end-to-end test is: 1 droplet, `subfinder` on a few domains, the job visible in Herdr.app, results queryable, teardown leaves nothing behind.

**Non-Goals:**
- Running any Temporal worker or Python code on droplets.
- Managing `~/.ssh/config` or the tailnet policy from code. Both are operator setup.
- Multi-region or multi-provider fleets, and job concurrency above 1 per machine.
- Rotating the over-scoped OAuth client. That is an operator action, and code requires only `auth_keys` and `devices:core`.

## Decisions

### D1. Temporal is the brain on the control plane; Herdr (`hc`) is the hands on droplets

All workflows and activities run on `warden-orchestrator-queue` in `warden-serve`. Activities drive `hc` and S3.

Alternatives considered:
- **Temporal workers on droplets.** Rejected: it needs droplets to reach Temporal, needs Python on the image, and leaves processes invisible.
- **herdr only, with no orchestrator.** Rejected: the operator wants runs to resume and fleets to be destroyed even across control-plane outages.
- **Terminal `herdr` instead of `hc`.** Rejected: it isn't the operator's UI (see Context).

### D2. Connectivity directions

```
 control plane (MacBook, tailnet)                       fleet machine (tag:warden-worker)
 +-------------------------------------+   tailscale ssh :22    +----------------------------+
 | warden-serve: MCP + orchestrator    | ---------------------> | tailscaled (ephemeral)     |
 |   HcDriver -------------------------+   (hc's transport)     | hc (copied by machine add  |
 | hc server + Herdr.app (operator UI) |                        |   --install, on demand)    |
 | temporal 127.0.0.1:7233 (local only)|                        |   terminal <job_id>:       |
 | SeaweedFS 127.0.0.1:8333            | <--------------------- |     warden-run ... -- cmd  |
 |   + tailscale serve tcp:8333        |   presigned GET/PUT    | subfinder, curl            |
 +-------------------------------------+   :8333 only           +----------------------------+
```

The control plane reaches droplets on 22 only. Droplets reach the control plane on 8333 only, and Temporal is never exposed. `tailscale serve` keeps SeaweedFS bound to loopback while publishing it on the tailnet. Binding it to `100.64.0.1` directly was rejected because it breaks if the IP changes, and `0.0.0.0` was rejected because it exposes the LAN.

### D3. HcDriver over the `hc` CLI

`work/hc.py` exposes `HcDriver`, which runs `hc ... --json` as a subprocess and parses the JSON. Non-zero exits become a typed `HcError` carrying the error kind and message. Its operations:

| Operation | Command |
|---|---|
| `add_machine(name, target)` | `hc machine add <target> --name <name> --install --json` (no `--temp`, see spike results) |
| `remove_machine(name)` | `hc machine rm <name> --json` (missing, `machine_unknown`, is success) |
| `machine_info(name)` | `hc machine ls --json`, filtered to `name` (`state`, `version`) |
| `run(machine, name, labels, argv)` | `hc run --on <machine> --bg --no-parent --name <name> --label k=v ... -- <argv>` |
| `get(addr)` | `hc get <addr> --json`; a missing terminal returns `None` |
| `list(labels)` | `hc ls --all --label k=v ... --json` |
| `interrupt(addr)` | `hc interrupt <addr> --json` |
| `remove(addr)` | `hc rm <addr> --json` (missing is success) |
| `exec(machine, argv, timeout)` | `hc exec --on <machine> --timeout <s> --json -- <argv>`, returning `(rc, output)` |
| `version()` | `hc --version` |

**Ownership guard.** Every mutating call checks its target first:
- machines must match `^wf-[a-z0-9]{8}-\d{2,}$`;
- terminals must carry the label `warden.job`.

Calls that fail the check raise before invoking `hc`. List calls always filter by a `warden.*` label.

`hc` handles SSH itself, connecting through the operator's OpenSSH config with its own control-master connections and `BatchMode=yes`. A single transport is therefore enough, and the `forward`/`ssh` split from the terminal-herdr plan is gone. The operator adds a one-time SSH stanza, which Warden never writes:

```
Host wf-*.tail1234.ts.net
  User root
  StrictHostKeyChecking accept-new
  UserKnownHostsFile ~/.ssh/known_hosts.warden
  LogLevel ERROR
```

On destroy, Warden prunes its hosts from that dedicated file only, with `ssh-keygen -R <host> -f ~/.ssh/known_hosts.warden`.

**`hc` spike results (task 1.5).** The fixtures are in `tests/fixtures/hc/`; the local `hc` was 0.1.17, and 0.1.18 after an app update.
- **`machine add --install` works unattended.** On a fresh droplet with no `hc`, it ran with stdin closed in about 7 s, installed `hc` and connected.
- **`--install` only reinstalls when the remote version is incompatible**, not merely older. A remote on 0.1.17 kept working with a local 0.1.18, and the app itself runs mixed versions (it offers an "Update" button). Readiness therefore requires `connected` and only logs a version difference.
- **`--temp` machines are hidden from Herdr.app's Settings → Machines list.** Warden registers fleet machines without `--temp`, so the operator sees them while the fleet exists. They are removed on destroy and identified by their `wf-*` name.
- **`hc run --bg`** returns `{"address": "<machine>/tN", "name", "state": "live", ...}`. The form `<machine>/<name>` is also accepted by `get`, `interrupt` and `rm`.
- **`hc get --json`** reports `state` (`live` or `finished`), `exit_code` (null when the process ended by a signal), `signal`, `reason`, `finished_ms`, `labels` and `foreground`.
- **`hc interrupt`** returns `{"key": "ctrl+c", "outcome": "exited"}`. `hc rm` returns `{"removed": "<address>"}`.
- **Errors** exit non-zero and print `{"error": {"kind", "message", "hint"}}`:
  - a missing terminal is `not_found` (exit 3);
  - an unknown machine is `machine_unknown` (exit 4), including for `machine rm` and `exec --on`.
- **`hc exec --json`** exits 0 even when the program fails. It returns `{"code", "signal", "stdout", "stderr", "timed_out", "truncated", "ms"}`, with **stdout and stderr base64-encoded**.

**Herdr.app (window).** Herdr.app has its own CLI, `herdr-app`, and an agent skill (`herdr-app --skill`) that documents how to show work in the window:

> A terminal started with hc opts into the window with `herdr.space=<id>`. `herdr.tab=<id>` chooses its tab; without it, Herdr creates a tab in that space. Unlabeled workers stay hidden.

`herdr-app do space.new --name N --machine M --folder F` creates a space that **lives on machine M**, and `herdr-app ls` lists spaces with their `machine`. `work/hc.py` adds `HerdrApp`:

| Operation | Effect |
|---|---|
| `ensure_space(machine)` | Id of the space `warden <machine>` on that machine; created with `space.new --machine` if missing (its first tab is a shell on the machine) |
| `close_spaces_on(machine)` | `space.close --confirmed` for every space on that fleet machine |

Each job's terminal is started with the label `herdr.space=<id>`, using the **machine-local** id. `ls` reports remote ids as `<machine>/<id>`; with that prefix, Herdr opens a separate space instead. Herdr then gives the terminal a tab under the machine, which Warden renames to the job id (`tab.rename`). The space's first tab, created by `space.new --tab shell`, is a shell on the machine.

Approaches tried and rejected in the spikes:
- **Editing the layout document directly** (`doc.set` on `herdr-app`, local or `--on` a machine). The document is private state of the app: it saved its in-memory layout over outside edits within minutes.
- **`--temp` machines.** The app hides them from Settings → Machines.

**Herdr drops a tab when its terminal's program exits.** So `run_job` starts `warden-run --hold`, which, after uploading the completion marker, replaces itself with a login shell. The job's output and summary stay on screen until the fleet is destroyed. Lost-job detection still works, because the shell starts only after the marker exists.

### D4. Per-fleet Pulumi stack contents

The stack is `warden-fleet-<fleet_id>`, in the existing local backend:

- `tailscale.TailnetKey`: `reusable=True` (N machines), `ephemeral=True`, `preauthorized=True`, `expiry=3600`, `tags=["tag:warden-worker"]`. Credentials come from the `TAILSCALE_OAUTH_CLIENT_*` environment variables. The tailnet is set explicitly from `WARDEN_TAILNET`, rather than relying on `-`, which the Pulumi provider docs don't confirm.
- `digitalocean.Droplet` × N:
  - Image `ubuntu-24-04-x64` and the existing size and region settings.
  - `user_data` is the cloud-init (D5), with the key injected as a Pulumi secret.
  - Tags `warden` and `fleet:<id>`.
  - `ssh_keys` keeps the existing account key fingerprint. That's only for break-glass access through the DigitalOcean console, and it stops DigitalOcean emailing root passwords.
- `digitalocean.Firewall` attached to the droplets: **no inbound rules**; outbound TCP, UDP and ICMP to anywhere. Tailscale works through NAT traversal and DERP using outbound traffic only (verified in the first spike).

`MachineEntry` gains a `tailnet_host` field (`wf-<id>-NN.<suffix>`). Inventory still comes from stack outputs and the sidecar `inventory.json` (`infra/state.py`); the `exec_mode` field goes away.

### D5. Cloud-init (pinned, idempotent, ordered for debuggability)

1. Install apt basics: `curl`, `ca-certificates`, `unzip`.
2. Tailscale: run the official install script, then `tailscale up --auth-key=<key> --hostname=wf-<id>-NN --ssh --advertise-tags=tag:warden-worker`. This runs early, so a later failure can still be debugged over Tailscale SSH (`cloud-init status --long`, `journalctl`).
3. subfinder `v2.6.7` from the GitHub release, sha256-verified (reusing the existing install snippet).
Neither `hc` nor `warden-run` is installed by cloud-init. The readiness gate copies both from the control plane over Tailscale SSH: `hc machine add --install`, then `hc file write` for `/usr/local/bin/warden-run`. Machines therefore always run the control plane's current versions.

The machine joins the tailnet before its tools are installed, so being online doesn't mean ready. The readiness gate therefore checks that `cloud-init status` reports `done` (D8).

### D6. Job contract: `warden-run`

The wrapper is a small bash script, kept in the repo as `src/warden/assets/warden-run.sh` and installed on each machine by the readiness gate (D8):

```
warden-run [--hold] --job <job_id> --timeout <secs> \
  --in <GET url> --out <PUT url> --log <PUT url> --done <PUT url> \
  -- <module template with {in} {out}>
```

1. Fetch the input into `/var/lib/warden/jobs/<job_id>/in.txt` (`curl -fsS --retry 5`).
2. Substitute `{in}` and `{out}` with local paths.
3. Run `timeout --signal=INT --kill-after=30 <secs> bash -c "<cmd>"`, writing output to `job.log`, which is tailed live into the terminal. The command's exit code comes from the `timeout` process.
4. A trap on `INT` (sent by `hc interrupt`) forwards the interrupt to the command and records the reason `cancelled`. Exit code 124 maps to `timeout`.
5. Upload the output (an empty file if the tool wrote none) and the log, then **last** upload `done.json`:
   ```json
   {"job", "rc", "rows", "reason", "started", "finished", "machine"}
   ```
   `rows` is the output line count, and `reason` is one of `ok`, `failed`, `timeout` or `cancelled`.
6. Exit with `rc`; with `--hold` (always used by Warden), `exec` a login shell instead, so the terminal and its tab stay open.

The control plane starts it with `hc run ... -- warden-run --job ... -- "<template>"`. It is passed as an argv, so the template stays one argument and nothing is typed into a shell.

Completion comes from the S3 marker, not from terminal state. That keeps one source of truth across resumes and survives `hc` losing a terminal. `hc get` is used only for liveness (D7).

### D7. `run_job` activity: resume semantics

`run_job(JobSpec)` runs on the orchestrator queue. A `JobSpec` is: `run_id`, `shard_id`, `attempt`, `machine`, `module`, and the presigned URLs. The terminal is named `job_id` and its address is `<machine>/<job_id>`. Steps:

1. **Already finished?** HEAD `a<n>.done.json`. If it exists, parse it and return the result.
2. **Look up the terminal** with `hc get <machine>/<job_id>`:
   - **live:** go to the wait step, without relaunching;
   - **finished with no marker:** the job died before reporting; raise `JobLost` (retryable);
   - **absent:** go to the launch step.
3. **Launch.** Run `hc run --on <machine> --bg --no-parent --name <job_id>` with the labels `warden.job=<job_id>`, `warden.run=<run_id>`, `warden.shard=<shard_id>` and `warden.attempt=<n>`, followed by `-- warden-run ...`. The terminal also gets the label `herdr.space=<id>` from `HerdrApp.ensure_space(machine)`. That is best effort: without a Herdr window the job runs hidden. Before launching, a finished terminal left by an earlier lost attempt with the same name is removed (`hc rm`).
4. **Wait.** Poll for the marker every 5 s and heartbeat. Every 30 s, check `hc get`. If the terminal is finished or gone and no marker has appeared within a 30 s grace period (for uploads), raise `JobLost`.
5. **On cancellation:** `hc interrupt <addr>`, wait up to 30 s for the marker, then re-raise.

Two kinds of failure are handled differently:
- **Infrastructure failures** (`hc` or SSH errors, `JobLost`, S3 errors) use **activity retries**. A retry goes through step 1 or 2 and never duplicates a live job.
- **Module failures** (`rc != 0`) are returned as **results, not exceptions**. The workflow then starts attempt `n+1` with a new job id and new keys.

This keeps the rule "a finished job is never relaunched" consistent with "failed shards are retried".

Activity timeouts: `start_to_close = module.timeout + 10 min`, `heartbeat_timeout = 2 min`.

### D8. Workflows

**`FleetWorkflow`** (id `fleet/<fleet_id>`) replaces `FleetOrchestratorWorkflow` and `FleetExecutorWorkflow`. The client starts it directly with `REJECT_DUPLICATE`, so starting a duplicate is a no-op.

States: `PROVISIONING → READY → DESTROYING → DONE`, plus `FAILED`.

```
PROVISIONING
  provision_fleet         (pulumi up)
  wait_fleet_ready        per machine:
                            Tailscale API: device online
                            -> hc machine add --install (retried until SSH answers)
                            -> hc machine ls: state connected (version recorded;
                               a difference from local hc is logged, not fatal)
                            -> hc exec: `cloud-init status` reports done
                            -> hc file write: install /usr/local/bin/warden-run
                            -> hc exec: curl PUT to presigned fleets/<id>/probe/<machine>,
                               then HEAD it
READY
  wait_condition(destroy_signal | deadline)        deadline = start + max_lifetime
  on CancelledError (parent REQUEST_CANCEL): fall through to destroy
DESTROYING
  unregister_machines     (close the machines' Herdr spaces, then hc machine rm;
                           best effort)
  destroy_fleet           (pulumi destroy)
  delete_tailnet_devices  (devices:core)
  prune_known_hosts       (best effort)
DONE | FAILED
```

- If the parent of a run-scoped fleet requests cancellation, the workflow catches `CancelledError` and runs the destroy activities before finishing.
- It reports `fleet_ready` / `fleet_failed` (with inventory) to the parent through an external-workflow signal when started as a child.
- Queries return status and deadline.
- Continue-as-new and `ExecutorState` are dropped: fleets live for hours and produce few events.

**`ReconRunWorkflow`** (id `run/<run_id>`). Inputs:

| Field | Meaning |
|---|---|
| `run_id`, `module` | Run identity and catalog module |
| `input_key` | Input already staged by the client |
| `fleet_id` *or* `instances` | Existing fleet, or size of a scoped fleet |
| `shard_count`, `max_attempts` | Sharding and retry settings |

Steps:
1. **Get a fleet.**
   - Scoped fleet: start a child `FleetWorkflow` with id `fleet/<run_id[:8]>` and `ParentClosePolicy.REQUEST_CANCEL`, then wait for the `fleet_ready` / `fleet_failed` signal.
   - Existing fleet: the `get_fleet` activity queries the fleet's status and requires `READY`.
2. **Shard.** The `plan_shards` activity reads `input.txt` from S3, writes `shards/<id>.in` and returns `[{shard_id, lines, machine_index}]`.
3. **Run.** One coroutine per shard, gated by one workflow-local `asyncio.Lock` per machine (deterministic). Each coroutine loops over attempts `1..max_attempts` calling `run_job`.
4. **Merge** (D10). Job terminals are not cleaned up per run. Every job's terminal and tab stays visible until its fleet is destroyed, and teardown closes the machines' spaces.
5. **Release the fleet.** If it is scoped, signal `request_destroy` to the child and await its completion.

The status query returns the shape in the `recon-runs` spec.

### D9. Object store: SeaweedFS in Docker plus `tailscale serve`

`deploy/compose.yaml` runs `chrislusf/seaweedfs:4.48` (`server -s3 -s3.port=8333 -dir=/data`):
- published on `127.0.0.1:8333`;
- data in `./state/seaweedfs`;
- S3 identity from `deploy/seaweedfs-s3.json`, generated from `WARDEN_S3_ACCESS_KEY` and `WARDEN_S3_SECRET_KEY`.

The operator publishes it once, and the setting persists in tailscaled. On Tailscale 1.50 the command is `tailscale serve tcp:8333 tcp://localhost:8333`; on 1.52 and later it is `tailscale serve --bg --tcp 8333 tcp://localhost:8333`.

`warden-serve` creates bucket `warden` at startup if it is missing. It uses two `boto3` clients with path-style addressing and SigV4:

- `WARDEN_S3_ENDPOINT=http://127.0.0.1:8333` for control-plane reads and writes;
- `WARDEN_S3_PUBLIC_ENDPOINT` (default `http://<WARDEN_CONTROL_HOST>:8333`) **only for presigning**.

Presigning uses the public endpoint because the signature covers the Host header that droplets will send. `tailscale serve --tcp` forwards at layer 4, so the Host header reaches SeaweedFS unchanged. This was verified from a real droplet in the first spike.

Alternatives considered: MinIO, rejected because its community edition no longer ships builds; Garage, rejected for more setup; plain HTTP file serving, rejected because it has no per-object signed access.

### D10. Merge and SQL with DuckDB, in-process

- **`merge_results` activity:**
  1. Download the successful attempts' `.out` files to a temp directory.
  2. With DuckDB, run `SELECT DISTINCT trim(line) AS value WHERE value <> ''` and write `result.parquet`.
  3. Upload it and return the row count.
- **`read_results`:** a paginated `SELECT value ... LIMIT/OFFSET` over a downloaded copy.
- **`query_results`:**
  1. Open a fresh in-memory connection.
  2. `CREATE TABLE results AS SELECT * FROM read_parquet('<tmp>')`.
  3. `SET enable_external_access=false; SET lock_configuration=true`.
  4. Run the operator's SQL and `fetchmany(10000)`.

  The in-memory copy means even write statements can't touch stored data. Tests must cover file-read and network attempts being blocked.

### D11. MCP tools (replaces the current set)

| Tool | Path |
|---|---|
| `list_modules` | Catalog |
| `start_fleet(instances, max_lifetime_minutes=120, region?)` | Temporal |
| `fleet_status(fleet_id)` | Temporal query + inventory |
| `destroy_fleet(fleet_id)` | Temporal signal |
| `run_module(module, targets? \| input_run_id?, fleet_id? \| instances=1, shard_count=0, max_attempts=2)` | Stages input to S3, then starts `ReconRunWorkflow` |
| `run_status(run_id)` | Temporal query |
| `wait_run(run_id)` | Temporal result |
| `cancel_run(run_id)` | Temporal cancel |
| `read_results(run_id, limit=100, offset=0)` | S3 + DuckDB directly |
| `query_results(run_id, sql)` | S3 + DuckDB directly |

Actions that change state go through Temporal. Result reads go straight to the store, so they work even after a run's workflow history has been cleaned up.

### D12. Module catalog as templates

`catalog.py` keeps `ModuleSpec` but replaces `binary`/`extra_args` with `command` (for example, `subfinder -dL {in} -silent -o {out}`) and adds `timeout_s` (subfinder: 1800). Validation rejects templates missing either placeholder.

### D13. Startup checks and orphan sweep

`warden-serve` startup runs these checks, failing fast with a clear message on any of them:
- Temporal is reachable;
- S3 is reachable and the bucket exists;
- `hc` is on PATH and `hc server status` answers;
- the Tailscale OAuth token exchange succeeds.

It then starts `SweepWorkflow` with fixed id `sweep/<date-hour>` (`ALLOW_DUPLICATE_FAILED_ONLY`). Its activity:
1. Lists DigitalOcean droplets tagged `warden` (REST API), tailnet devices tagged `tag:warden-worker` (Tailscale API), and `hc` machines named `wf-*`.
2. Lists running `FleetWorkflow`s through visibility (`WorkflowType='FleetWorkflow' AND ExecutionStatus='Running'`).
3. For each orphaned fleet id, runs `pulumi destroy` on its stack if one exists, otherwise deletes the droplets directly. It then deletes matching tailnet devices and `hc machine rm`s matching machines.

Droplets from older Warden versions, which are tagged `warden` / `fleet:<id>`, are covered by the same sweep.

### D14. Identifiers and `hc` layout

| Thing | Format |
|---|---|
| `fleet_id` | `[a-z0-9]{8}` (unchanged) |
| `run_id` | 12 hex characters |
| Run-scoped `fleet_id` | `run_id[:8]` |
| Job id | `<run_id>-<shard_id>-a<n>`, which fits `hc`'s 32-character terminal-name limit for up to 999 shards |
| `hc` machine | `wf-<fleet_id>-NN`, registered without `--temp` so it is listed in Herdr.app; target `root@wf-<fleet_id>-NN.<suffix>` |
| `hc` terminal | name = job id, address `wf-<fleet_id>-NN/<job_id>` (or the `tN` address `hc run` returns) |
| Terminal labels | `warden.job`, `warden.run`, `warden.shard`, `warden.attempt` |
| Herdr.app space | `warden <machine>`, living on that machine; one tab per job |

## Risks / Trade-offs

- **[`hc` is young (0.1.17) and its JSON may change]** → Keep all parsing in `work/hc.py`, test against recorded fixtures from the `hc` spike, and log the `hc` version at startup. `--install` keeps the remote version equal to the local one.
- **[Warden mistakenly touching the operator's own `hc` terminals or machines]** → The ownership guard in D3 (name pattern and `warden.job` label) is enforced before any mutating call and covered by unit tests.
- **[Herdr.app's commands change between releases]** → Use only `herdr-app ls` and `do space.new|space.close`, from its own agent skill. All Herdr calls are best effort, so jobs never depend on the window.
- **[Laptop sleeps while a fleet is running]** → Droplets bill until it wakes. Mitigations:
  - the default maximum lifetime is 2 h, and the timer fires immediately on wake (Temporal is persisted);
  - the startup sweep catches anything whose workflow was lost.

  Accepted for this first test. An always-on control host is a follow-up.
- **[OAuth client has scope `all`, and its secret appeared in a chat transcript]** → Code uses only the `auth_keys` and `devices:core` endpoints. The operator should rotate to a credential with only those scopes; nothing in code changes.
- **[The policy's `hosts.warden-control` is a fixed IP]** → If the laptop re-registers and its IP changes, workers lose S3 access. The policy tests don't catch that, so the readiness gate also performs one presigned S3 round-trip from each machine before reporting `READY`.
- **[Presigned URLs are visible in terminal scrollback and process lists on the droplet]** → They cover one key, expire, and the droplet belongs to the fleet anyway.
- **[Single-node SeaweedFS on the laptop disk]** → No redundancy; acceptable for recon output during this phase.
- **[DuckDB sandbox settings]** → Covered by explicit tests (file read, `httpfs`, `COPY TO`). If any escape is found, drop the `sql` tool and keep only paginated reads.
- **[Cloud-init boot takes about 2–4 minutes]** → The readiness timeout is 15 minutes. A Packer snapshot is the follow-up that removes this.

## Migration Plan

1. Run `git init` and commit the current tree, so this change is revertible. Done: commit `c1e7901`.
2. Operator one-time setup:
   - Herdr.app (`hc`) running;
   - add the SSH stanza (D3);
   - set the new `.env` keys (`WARDEN_TAILNET`, `WARDEN_TS_SUFFIX`, `WARDEN_CONTROL_HOST`, the S3 keys and endpoints);
   - `docker compose -f deploy/compose.yaml up -d`, then `tailscale serve` (D9).
3. Deploy the code. No live Warden droplets exist (checked during planning). Old Pulumi stacks in `state/pulumi/` from earlier fleets are inert; the sweep and `pulumi destroy` handle any that still hold resources.
4. Leave the old local Flight datasets under `data/` unused; they can be deleted by hand.
5. **Rollback:** `git checkout` the previous commit. The tailnet policy and SeaweedFS can stay, since the old code ignores them. The terminal `herdr` upgrade to 0.9.5 (from the first spike) is unrelated to Warden and can stay.

## Open Questions

- Whether successful-run terminals should be configurable to stay listed. Default is to remove them; this can be revisited without changing the specs.
