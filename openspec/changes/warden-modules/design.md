# Design

## Context

Motivation is in `proposal.md`, and requirements are in `specs/`. This change builds on `herdr-fleet-tailscale` (D-numbers below prefixed `HFT-` refer to its design). These parts of that change are kept as they are:
- the Pulumi stack (HFT-D4), cloud-init ordering (HFT-D5), the `hc` driver and Herdr.app integration (HFT-D3);
- the fleet workflow's lifetime and teardown (HFT-D8) and the orphan sweep (HFT-D13).

Facts this design relies on:
- **The `hc` driver works and is tested against fixtures.** `hc ls --label` lists terminals on remote machines, `hc kill <t>` ends a terminal's program (hangup, then escalate), and `hc run --cwd` sets the working directory.
- **Herdr.app drops a tab when its program exits.** The proven fix is to `exec` a login shell after the program ends (HFT-D6 `--hold`).
- **Two Temporal servers exist on the laptop.** The dev server is on `127.0.0.1:7233`. `localhost:7233` resolves to `[::1]`, which is a separate Docker Desktop Temporal extension.
- **The tailnet policy is operator-owned.** Today it grants `tag:warden-worker` access to `warden-control:8333` only.
- **The live baseline is fleet `5bzuh2z9`.** subfinder on `example.com` gave 507 rows in 15.9 s through raw `hc`.

## Goals / Non-Goals

**Goals:**
- **Warden doesn't know what a module does.** The only contract is `entrypoint.sh` plus the environment (`fleet-modules`).
- **Warden stores no state about modules or runs.** Module state comes from `hc` terminals on the machines; run state comes from Temporal.
- **A new Temporal capability is two things:** a module folder and one hand-written tool function.
- **About half of today's source goes away,** with no new dependencies on the control plane.

**Non-Goals:**
- Validating module contents beyond `entrypoint.sh`, or sandboxing modules: they run as root on disposable droplets.
- Pinning or versioning module code across a fleet, beyond the "ship only when no slot is live" rule (D3).
- Supporting non-DigitalOcean providers or more than one Temporal namespace.

## Decisions

### D1. Two kinds of Temporal workers

```
 control plane (laptop)                                     fleet machine wf-<id>-NN
 +-------------------------------------------+              +---------------------------------+
 | warden-serve                              |  hc over     | space "warden wf-<id>-NN"       |
 |   MCP (fleet_*, module_*, temporal_*)     |  tailscale   |   tab shell                     |
 |   worker: warden-orchestrator-queue       |  ssh :22     |   tab "subdomains #1"           |
 |     FleetWorkflow, SweepWorkflow,         | -----------> |     entrypoint.sh -> worker.py  |
 |     fleet activities                      |              |       queue <id>.subdomains     |
 | Temporal dev server 127.0.0.1:7233        |              |   tab (raw hc, agent-started)   |
 |   + tailscale serve tcp:7233              | <----------- |                                 |
 | hc server + Herdr.app                     |  gRPC :7233  +---------------------------------+
 +-------------------------------------------+
```

- **Control-plane worker** (in `warden-serve`, queue `warden-orchestrator-queue`). It runs only Warden's own workflows: fleet lifecycle and sweep. These must run where Pulumi credentials live and must survive the fleet.
- **Fleet workers.** Whatever a module's entrypoint starts. For Temporal modules, that is a worker polling `<fleet_id>.<module>` that runs both the module's workflows and its activities.

Alternative considered: module workflows on the control plane, with only activities on the fleet. A run would then survive its fleet. Rejected by the operator: a module should be a self-contained folder that Warden doesn't import, so its workflow code must live with it. The consequence, that a run cannot outlive its fleet, is handled in D7.

### D2. Exposing Temporal to the fleet

- **Publishing.** The operator runs `tailscale serve tcp:7233 tcp://127.0.0.1:7233` (Tailscale 1.50; on 1.52+ `tailscale serve --bg --tcp 7233 tcp://127.0.0.1:7233`). The target must be `127.0.0.1`, never `localhost`, because of the Docker Desktop extension on `[::1]`.
- **Binding.** The dev server stays bound to loopback.
- **Policy.** The grant `tag:warden-worker → warden-control:8333` becomes `:7233`. Policy tests assert that workers reach 7233 and cannot reach 22, 8233 or 8333.
- **Worker address.** Workers get `TEMPORAL_ADDRESS` from `WARDEN_TEMPORAL_FLEET_ADDRESS`, which defaults to `<WARDEN_CONTROL_HOST>:7233` (the MagicDNS name; `tailscale up` accepts tailnet DNS by default on Linux).
- **Readiness probe.** The gate's last stage is `hc exec --on <m> -- timeout 5 bash -c '</dev/tcp/<host>/7233'`. That one check covers DNS, the policy grant and `tailscale serve`, and replaces the old S3 probe for the same reason: the policy pins a fixed IP.

Alternatives: bind Temporal to `0.0.0.0` (exposes it to the LAN), or bind to the tailnet IP (breaks if the IP changes). Both were rejected for the same reasons as for S3 in HFT-D2.

### D3. Module delivery

1. **Build.** `modules.py` builds a gzip tarball of the module folder in memory, excluding `.venv`, `__pycache__`, `.git` and `*.pyc`.
2. **Upload.** It writes the tarball with `hc file write --on <m> /tmp/warden-module-<module>.tgz`; stdin carries the bytes.
3. **Unpack.** `hc exec` extracts it into `/opt/warden/modules/<module>.new` and swaps it into place with `rm -rf` and `mv`.

Task 1.2 confirms that `hc file write` is binary-safe. If it isn't, the fallback is base64 on the wire and `base64 -d` on the machine.

**Ship only when no slot of that module is live on the machine.** Scale-up reuses the copy already there. Changing a running module's code is always `module_deactivate`, then `module_activate`. This avoids mixing old and new workflow code on one queue, which would break Temporal replay.

### D4. Worker terminals

For slot `n` on machine `m`, the module terminal is started like this:

```
hc run --on m --bg --no-parent --name <module>-<n> --cwd /opt/warden/modules/<module> \
  --label warden.fleet=<id> --label warden.module=<module> --label warden.slot=<n> \
  --label herdr.space=<machine-local space id> \
  -- env WARDEN_FLEET=.. WARDEN_MACHINE=.. WARDEN_MODULE=.. WARDEN_WORKER=n \
         WARDEN_WORKER_ID=m/<module>-<n> WARDEN_TASK_QUEUE=<id>.<module> TEMPORAL_ADDRESS=.. \
     bash -c 'rm -f "$RC"; ./entrypoint.sh; rc=$?; echo "$rc" > "$RC";
              printf "\n[warden] %s #%s exited rc=%s\n" "$WARDEN_MODULE" "$WARDEN_WORKER" "$rc";
              exec bash -l'
```

`RC` is `/var/lib/warden/slots/<module>-<n>.rc`.

- **Exit visibility.** The trailing `exec bash -l` keeps the tab open after an exit; this is the proven `--hold` pattern.
- **Exit codes.** The rc file is how status tells "exited (rc)" apart from "running" while the terminal stays live.
- **Tab name.** After launch, `HerdrApp.name_tab` renames the tab to `<module> #<n>`.
- **Missing space.** `HerdrApp.ensure_space` is called first and is best effort. Without a space id, the label is omitted and the slot runs hidden.

Rejected: `hc run --keep`. It replaces programs that were *lost*, never re-runs the command, and its behaviour on a normal exit isn't documented. The explicit shell is proven.

**Deactivation**, for each matching terminal:
1. `hc interrupt` (ctrl+c lets a Temporal worker shut down gracefully);
2. wait up to 30 s for the rc file;
3. `hc kill`;
4. `hc rm`.

The ownership guard in `hc.py` changes from job terminals to module terminals:
- the name must match `^[a-z][a-z0-9-]{0,23}-\d{1,2}$`;
- the terminal must carry `warden.module`;
- it must live on a `wf-*` machine.

### D5. Module state from the runtime

`module_state(fleet)` builds its answer from two sources, with no stored records:
1. **Terminal listing:** `hc ls --label warden.fleet=<id>`, grouped by machine (from the address) and by module (from the name).
2. **Exit codes:** one `hc exec` per machine that reads `/var/lib/warden/slots/*.rc`. Machines are queried in parallel.

A slot is:
- **running** when its terminal is live and has no rc file;
- **exited(rc)** when the rc file exists;
- **absent** otherwise.

The rule: what is running comes from the runtime, never from a file. It also means a `warden-serve` restart loses nothing.

### D6. Activation paths

Both paths call the same idempotent function, `modules.activate(fleet_machines, module, workers)`.

- **From the MCP** (`module_activate`, and the `temporal_` tools through `ensure_active`), the call runs directly in a thread. It is safe to retry and has no state to make durable.
- **From `FleetWorkflow`**, after `READY`, an `activate_modules` activity runs when the fleet spec lists modules. Its per-module errors are stored in the workflow and exposed through the status query.

Alternative: route every activation through a FleetWorkflow Update. That makes activation durable at the cost of handler bookkeeping. It was rejected because activation is idempotent and its state lives on the machines anyway.

### D7. Runs: start, observe, terminate

`runs.py` holds the Temporal-facing helpers. `mcp.py` stays a list of thin tool functions.

**Starting a run, `start_run(fleet, module, workflow, arg, timeout)`:**
1. **Resolve the fleet.** Use the given id, or the only `READY` fleet, via `fleet_list`.
2. **Ensure the module is active.** Activate it with 1 worker if no slot is running.
3. **Wait for pollers.** Call `describe_task_queue` for both the workflow and activity task types of `<id>.<module>` every 2 s, for up to 300 s. Done when every running slot's `WARDEN_WORKER_ID` appears among the pollers' identities. Module workers must set `identity=WARDEN_WORKER_ID`; this is part of the contract documented in the module README.
4. **Start the workflow** by name (a string, never an import), with:
   - id `run/<id>/<module>-<6 hex>`;
   - task queue `<id>.<module>`;
   - `execution_timeout = min(timeout, deadline - now)`.

**Status, result, wait and cancel:**
- **Status** uses `handle.describe()` for the status, times and task queue, and the raw description's pending activities.
- **Result** uses `handle.result()` when the run is closed. Above 20,000 characters, the JSON goes to `state/results/<id>-<module>-<hex>.json`.
- **Wait** is `asyncio.wait_for(handle.result(), timeout)`; on timeout it falls back to status.
- **Cancel** is `handle.cancel()`.
- **Guard.** Every one of these refuses ids that don't start with `run/`.

**Terminating runs at teardown.** `terminate_fleet_runs(fleet_id)` is the first teardown activity:
1. list `ExecutionStatus='Running'` workflows;
2. keep ids starting with `run/<fleet_id>/` (filtered on the client, to avoid relying on `STARTS_WITH` support in the dev server's SQLite visibility);
3. terminate each with reason `fleet <id> destroyed`.

The deactivation guard uses the same listing, filtered to `run/<fleet_id>/<module>-`.

### D8. FleetWorkflow changes

```
PROVISIONING  provision_fleet
              wait_fleet_ready: tailnet -> register (hc machine add) -> connected
                                -> boot (cloud-init done) -> space (best effort) -> temporal probe
READY         activate_modules (if spec.modules)   errors -> status.module_errors
              wait_condition(destroy_signal | deadline)
DESTROYING    terminate_fleet_runs -> unregister_machines (close spaces, hc machine rm)
              -> destroy_fleet -> delete_tailnet_devices -> prune_known_hosts
```

**Removed:**
- the parent signalling (`_notify_parent`, `fleet_ready` / `fleet_failed`);
- the run-scoped mode;
- the `install` stage (warden-run) and the `probe` stage (S3).

**Kept:** handling of `CancelledError`, which still destroys the fleet, as a defensive path.

`FleetSpec` gains `modules: list[str]`. `FleetStatus` gains `module_errors`.

### D9. Activities as classes with injected collaborators

`dependencies.py`, the process-wide service locator, goes away. Fleet activities become methods of `FleetActivities(settings, hc, herdr, tailscale, client, provider)`, built once in `serve.py` and passed to the `Worker`. Tests build it with the fakes from `tests/fakes.py`. This is the Temporal Python SDK's recommended way to give activities dependencies, and it removes the global `install()` / `reset()` dance from tests.

### D10. Code layout

```
src/warden/
  config.py          settings (- S3, + modules_dir, temporal_fleet_address)
  hc.py              HcDriver + HerdrApp (moved from work/; module-terminal guard; + kill)
  tailscale.py       Tailscale API (renamed from tailscale_api.py)
  fleet/
    models.py        FleetSpec, FleetState, MachineEntry, FleetStatus, FleetResult, ids
    provider.py      Pulumi DigitalOcean program + cloud-init (uv instead of subfinder)
    activities.py    FleetActivities: provision, ready, activate_modules, terminate_fleet_runs,
                     unregister, destroy, tailnet cleanup, known_hosts, sweep
    workflow.py      FleetWorkflow, SweepWorkflow
    client.py        start / list / status / destroy fleets from the Temporal client
  modules.py         discover, describe, pack, activate, deactivate, module_state
  runs.py            resolve_fleet, ensure_active, wait_pollers, start_run, status/result/wait/cancel
  mcp.py             the 12 hand-written tools
  serve.py           startup checks (- store), control-plane worker, MCP
modules/
  README.md          the contract: entrypoint.sh, environment, worker identity
  subdomains/
    entrypoint.sh    install pinned subfinder, then `exec uv run --script worker.py`
    worker.py        PEP 723 (temporalio>=1.35); identity, queue, max_concurrent_activities=1
    workflows.py     SubdomainEnum + enumerate_chunk activity
```

`runs.py` is split out of `modules.py` so that `modules.py` only talks to `hc` and `runs.py` only talks to Temporal.

The Pulumi inventory sidecar (`infra/state.py`) is dropped: nothing read it. Stack folders live in `provider.py`, which also answers `has_stack` for the sweep. `mcp.py` builds its tools around an `App` (settings, Temporal client, Herdr.app driver, modules) that `serve.py` constructs.

**Deleted:** `work/`, `infra/`, `providers/` (merged into `fleet/`), `assets/`, `catalog.py`, `store.py`, `client_api.py`, `dependencies.py`, `models.py`, `mcp_server.py`, `deploy/`.

### D11. The `subdomains` module

**`entrypoint.sh`:**
- The first comment line is the description.
- Installs subfinder `v2.6.7` when `subfinder -version` doesn't report it: download the release zip, check SHA-256 `d988a481d3037c55e685afee023eb104a81a77dd2691fb902b59019a365f6103`, unzip to `/usr/local/bin`.
- Then runs `exec uv run --script worker.py`. Concurrent slots share uv's cache, and uv locks it.

**`worker.py`:**
- Connects to `TEMPORAL_ADDRESS` with `identity=WARDEN_WORKER_ID`.
- Runs a `Worker` on `WARDEN_TASK_QUEUE` with `max_concurrent_activities=1` (one chunk per slot, so the tab shows one tool at a time).
- Shuts down gracefully on SIGINT or SIGTERM.

**`workflows.py`, workflow `SubdomainEnum(domains, chunk_size=25)`:**
- Deduplicates defensively.
- Runs one `enumerate_chunk` per chunk concurrently, each with `start_to_close=30m`, `heartbeat_timeout=60s` and `RetryPolicy(maximum_attempts=2)`.
- Gathers the chunks with exceptions captured.
- Fails non-retryably if every chunk failed.
- Fails with a clear message if the merged result would exceed 1.5 MB.
- Otherwise returns `{subdomains, count, chunks: {total, succeeded, failed}, failed_chunks: [{domains, error}]}`.

**`workflows.py`, activity `enumerate_chunk`:**
1. Prints a header line (run id and chunk `i/N`).
2. Writes the domains to a temporary file.
3. Runs `subfinder -dL in -silent -o out` in its own process group, with stdout and stderr inherited so the output streams into the tab.
4. Heartbeats every 10 s.
5. On `CancelledError`, kills the process group and prints a cancelled line before re-raising.
6. On a non-zero exit, raises a retryable `ApplicationError`.
7. Returns the lowercased lines, after printing a footer with the outcome and count.
8. Fails non-retryably when its own result would exceed 1.5 MB, asking for a smaller `chunk_size`. One chunk alone can pass Temporal's 2 MB payload limit, which otherwise fails with an opaque upload error after a pointless retry.

**Normalisation** (trim, lowercase, drop blanks and `#` lines, deduplicate, hostname regex, 1–10,000 domains) happens in the MCP tool, so bad input fails before a run starts.

### D12. Identifiers

| Thing | Format |
|---|---|
| Module name | `^[a-z][a-z0-9-]{0,23}$` (folder name) |
| Task queue | `<fleet_id>.<module>` |
| Module terminal | name `<module>-<n>`; labels `warden.fleet`, `warden.module`, `warden.slot`, `herdr.space` |
| Herdr tab | `<module> #<n>` in space `warden <machine>` |
| Worker identity | `<machine>/<module>-<n>` |
| Slot exit file | `/var/lib/warden/slots/<module>-<n>.rc` |
| Module copy | `/opt/warden/modules/<module>` |
| Run id | `run/<fleet_id>/<module>-<6 hex>` |
| Large result | `state/results/<fleet_id>-<module>-<6 hex>.json` |

Fleet and machine identifiers are unchanged (HFT-D14).

## Risks / Trade-offs

- **[Temporal has no authentication, and fleet machines can now reach it]** → A compromised droplet could read histories or terminate workflows, including a `FleetWorkflow`. That would cancel its deadline timer.
  - Mitigations: the startup sweep; fleets are short-lived; the tailnet grant allows only 7233.
  - Accepted for laptop testing. mTLS with a per-fleet namespace is the follow-up.
- **[A run cannot outlive its fleet]** → Its execution timeout is capped at the fleet deadline, and teardown terminates it with a named reason (D7). Agents see `TERMINATED`, not a hang.
- **[Payload size limits]** → Temporal rejects payloads over 2 MB.
  - Inputs are capped at 10,000 domains.
  - Per-chunk results are small.
  - The workflow fails clearly above 1.5 MB.
  - A claim-check codec is the follow-up if a module needs more.
- **[Module code changing under live workers breaks replay]** → Ship only when no slot is live (D3), and refuse deactivation while runs are in flight unless forced.
- **[First activation downloads uv packages and the tool on every machine]** → It is slower and depends on GitHub and PyPI reachability. The poller wait allows 5 minutes. A Packer image is the follow-up.
- **[`hc file write` may not be binary-safe]** → Task 1.2 checks this first, with a base64 fallback.
- **[Graceful worker shutdown on `hc interrupt`]** → If a module ignores ctrl+c, deactivation waits 30 s and then uses `hc kill`.
- **[Tailscale `serve` syntax differs between versions]** → Document both forms. The readiness probe fails with the address named if it isn't working.

## Migration Plan

1. Close `herdr-fleet-tailscale`: mark tasks 8.1, 8.3 and 8.5 as superseded by this change, and archive it so its specs land in `openspec/specs/`. This change's deltas modify and remove those specs.
2. Operator, one-time:
   - `tailscale serve tcp:7233 tcp://127.0.0.1:7233`;
   - in the tailnet policy, swap the 8333 grant for 7233 and update the policy tests;
   - stop SeaweedFS (`docker compose -f deploy/compose.yaml down`) and remove its `tailscale serve tcp:8333`.
3. Deploy the code and restart `warden-serve`. Claude Code picks up the new tool list when the MCP server reconnects.
4. No fleet should be up during the switch. Any leftover is caught by the sweep.
5. **Rollback:** `git checkout` the previous commit, re-enable SeaweedFS and its serve entry, and restore the 8333 grant.

## Open Questions

- Whether `fleet_status` should also include Temporal UI links for runs. It's cosmetic, and can be added without changing the specs.
