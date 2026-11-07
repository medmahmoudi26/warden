# Proposal

## Why

`herdr-fleet-tailscale` made fleets work end to end, but its work layer is heavy for what it does. Every recon job flows through:
- a control-plane orchestrator;
- a bash job wrapper;
- presigned S3 URLs and S3 completion markers;
- a shard planner;
- Parquet merges and a DuckDB SQL sandbox.

Running subfinder directly with `hc` on the same fleet gave the same rows in 16 seconds with none of that.

The operator wants a different shape:
- the fleet runs the code;
- each capability is a self-contained module folder whose processes are visible in Herdr;
- agents get a few hand-written MCP tools for common workflows such as subdomain enumeration, and use `hc` directly for anything else.

Warden will also host agent swarms, so a module cannot assume Temporal.

## What Changes

- **Modules are folders with an `entrypoint.sh`.**
  - Activating a module on a fleet copies its folder to every machine and starts `entrypoint.sh` in a named, labelled `hc` terminal per worker slot. Each terminal is shown as a tab in the machine's `warden <machine>` space in Herdr.app.
  - Warden doesn't care what the entrypoint runs. A Temporal worker, a long-running tool or an agent loop are all valid.
  - Modules live in `modules/` (overridable by `WARDEN_MODULES_DIR`).
- **Workflows and activities run on the fleet.** A Temporal-based module's entrypoint starts a Temporal worker that connects to the control plane's Temporal over the tailnet and polls `<fleet_id>.<module>`. Temporal then spreads activities across machines, retries them, times them out and cancels them. That replaces Warden's own sharding, job resume, lost-job detection and completion markers.
- **Hand-written MCP tools with prefixes:**
  - `fleet_*`: `up`, `list`, `status`, `down`;
  - `module_*`: `list`, `activate`, `deactivate`;
  - `temporal_*`: tools that wrap the Temporal client. The first is `temporal_subdomain_enumeration`, alongside `temporal_status`, `temporal_wait`, `temporal_result` and `temporal_cancel`.

  Nothing is generated from module metadata.
- **The first module is `modules/subdomains`.** It installs a pinned subfinder, runs a Temporal worker, and provides a `SubdomainEnum` workflow that chunks domains across the fleet and returns the deduplicated subdomains.
- **Several fleets can be up at once.** They are listed with `fleet_list`, and each fleet's modules use their own task queues.
- **Each machine gets a Herdr space when the fleet is ready.** `fleet_status` reports the space label, so agents and the operator can run raw `hc` commands that show up beside the module tabs.
- **Teardown first terminates the runs Warden started on that fleet's queues,** then closes the Herdr spaces (ending module terminals), then destroys the machines as before.
- **Tailnet:** workers reach only the control plane's Temporal port, published with `tailscale serve`. The S3 port grant goes away.
- **Cloud-init installs `uv` instead of subfinder.** Modules install their own tools, so the base image stays generic.
- **BREAKING:** remove the work layer:
  - `work/jobs.py`, `work/shards.py`, `work/results.py`, `work/recon.py`;
  - `assets/warden-run.sh`, `catalog.py`, `store.py`, `client_api.py`, and the run and job models;
  - the SeaweedFS deployment (`deploy/`), presigning, Parquet and DuckDB.
- **BREAKING:** MCP tools change.
  - Removed: `list_modules`, `start_fleet`, `destroy_fleet`, `run_module`, `run_status`, `wait_run`, `cancel_run`, `read_results`, `query_results`.
  - Replaced by the prefixed set above.
- **BREAKING:** run-scoped fleets are removed. A fleet is always started explicitly, and runs target an existing fleet.
- **Out of scope:**
  - generating MCP tools from module metadata;
  - agent swarm modules;
  - auto-restarting crashed entrypoints;
  - a shared module library;
  - a large-payload store (claim check);
  - Temporal authentication (mTLS) and per-fleet namespaces;
  - Packer images and moving the control plane off the laptop.

## Capabilities

### New Capabilities

- `fleet-modules`: The module contract and its lifecycle on a fleet.
  - **Contract:** a folder with an `entrypoint.sh`, and the environment Warden provides to it.
  - **Lifecycle:** discovery and listing; activation (ship, launch worker terminals, show them in Herdr); exit behaviour; deactivation; activation at fleet start.
  - **Ownership:** module terminals never touch the operator's own terminals or spaces.
- `mcp-tools`: The agent-facing MCP surface.
  - The `fleet_`, `module_` and `temporal_` prefixes, hand-written tools only.
  - Fleet auto-selection.
  - Starting a Temporal-backed run: ensure the module is active, wait for its workers to poll, then start the workflow.
  - Run status, wait, result and cancel.
  - The raw `hc` path agents can use instead.
- `subdomain-enumeration`: The `subdomains` module and its `temporal_subdomain_enumeration` tool. Covers input normalisation, chunked fan-out across the fleet, per-chunk retries and timeouts, visible tool output, and the deduplicated result.

### Modified Capabilities

These are defined by the not-yet-archived change `herdr-fleet-tailscale`, which must be archived before this one.

- `fleet-lifecycle`:
  - The readiness gate drops the job wrapper and the S3 probe, and adds the Herdr space and Temporal reachability.
  - Operator visibility gains a per-machine space.
  - Teardown terminates the fleet's runs first.
  - Fleet status reports spaces and active modules.
  - Several fleets can run at once.
  - Run-scoped fleets are removed.
- `job-execution`: All requirements are removed. Module terminals (`fleet-modules`) and Temporal activities replace Warden-managed jobs.
- `recon-runs`: All requirements are removed. Module workflows (`subdomain-enumeration`) and the `temporal_*` tools (`mcp-tools`) replace catalog runs.
- `result-store`: All requirements are removed. Results travel as Temporal payloads, and large ones are written to a local file by the MCP tool.

## Impact

- **Code:**
  - **Removed:** `src/warden/work/{jobs,shards,results,recon}.py`, `assets/`, `catalog.py`, `store.py`, `client_api.py`, `dependencies.py`, `deploy/`, and their tests.
  - **Restructured:**
    - `src/warden/fleet/` holds the provider, activities, workflow and models, built from `providers/`, `infra/` and `models.py`;
    - `hc.py` is moved up from `work/`, with the job guard replaced by a module-terminal guard;
    - `tailscale.py` (renamed from `tailscale_api.py`).
  - **New:** `src/warden/modules.py`, `src/warden/mcp.py` (replaces `mcp_server.py`), and `modules/subdomains/`.
  - **Unchanged in role:** `config.py` and `serve.py`.
- **Dependencies:** remove `boto3` and `duckdb`. The `subdomains` module declares `temporalio` inline (PEP 723) and runs under `uv` on the machines.
- **Control plane:**
  - SeaweedFS and its `tailscale serve tcp:8333` are retired.
  - Temporal stays bound to `127.0.0.1:7233` and is published to the tailnet with `tailscale serve` on 7233, forwarding to `127.0.0.1`, not `localhost`.
- **Tailnet policy (operator):** replace the `tag:warden-worker → warden-control:8333` grant with `:7233`. Update the policy tests: workers can reach 7233 and cannot reach 22, 8233 or 8333.
- **Configuration:**
  - Remove the `WARDEN_S3_*` settings.
  - Add `WARDEN_MODULES_DIR` (default `modules`) and `WARDEN_TEMPORAL_FLEET_ADDRESS` (default `<WARDEN_CONTROL_HOST>:7233`).
- **Security:** fleet machines can now talk to the control plane's Temporal, which has no authentication. A compromised droplet could read or terminate any workflow in the namespace. This is accepted for laptop testing; the fixed maximum lifetime and the startup sweep are the backstops.
