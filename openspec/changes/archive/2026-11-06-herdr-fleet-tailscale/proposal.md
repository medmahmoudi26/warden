# Proposal

## Why

Warden is meant to run work on a fleet of DigitalOcean droplets in the spirit of a `fleet.up` command. Today it can't: droplets have no way to reach the laptop control plane, so the DigitalOcean path pushes work over raw `ssh`/`scp` from an activity on the laptop (`work/ssh_exec.py`). Those processes are invisible, die with the connection and can't be inspected. A separate Arrow Flight server carries data that S3 and Temporal can handle on their own. Warden will also host agent swarms, which need a live, attachable terminal per process. The operator's Herdr.app (`hc`) already provides that, and Tailscale now connects the laptop and the droplets privately.

## What Changes

- **Execution moves to Herdr (`hc`).** Each fleet machine is registered with the operator's `hc` (and listed in Herdr.app), and `hc` copies itself onto the machine over Tailscale SSH. Warden starts each recon job as a named, labelled `hc` terminal on a droplet, shown as a tab in a `warden <machine>` space on that machine in Herdr.app (via its documented `herdr-app` commands), where it stays until the fleet is destroyed. Droplets run no Temporal worker and no Python.
- **Temporal stays as the orchestrator, on the control plane only.** It owns fleet lifecycle, run fan-out and merge, retries, timeouts and guaranteed teardown. Activities are thin herdr and S3 drivers on the orchestrator queue, and droplets never connect to Temporal.
- **Fleet machines join the tailnet.** Each fleet's Pulumi stack mints its own ephemeral, preauthorized auth key tagged `tag:warden-worker`, installs Tailscale with SSH enabled, and attaches a DigitalOcean firewall with no inbound rules. The tailnet policy (already applied) lets workers reach only S3 on the control plane, and lets the operator reach workers over Tailscale SSH.
- **S3 becomes the data plane.** A SeaweedFS instance on the control plane, published to the tailnet with `tailscale serve`, holds run inputs, per-job outputs, logs and completion markers. Droplets use only presigned URLs and hold no S3 credentials. Merged results are stored as Parquet and queried through DuckDB.
- **Fleets follow the `fleet.up` lifecycle model.** Fleets can be explicit (start and destroy) or scoped to a recon run, which provisions its own fleet and destroys it when the run ends, fails or is cancelled. Every fleet has a durable maximum lifetime. At startup Warden also sweeps orphaned droplets and tailnet devices.
- **Modules are axiom-style command templates.** A module is a template with `{in}`/`{out}` placeholders, run through a small `warden-run` wrapper installed on each droplet. subfinder is the only module shipped.
- **BREAKING:** remove the Arrow Flight server and client (`flight_server.py`, `flight_client.py`, `flight_store.py`, port 8815) and local Parquet datasets under `data/`.
- **BREAKING:** remove SSH exec mode (`work/ssh_exec.py`, `exec_mode`, `wait_for_ssh`), the Docker provider (`providers/docker.py`, `Dockerfile.worker`) and the fleet worker entrypoint (`warden-worker`, `worker.py`). DigitalOcean becomes the only provider.
- **BREAKING:** MCP tools change. `ingest_targets`, `query_dataset`, `dataset_info`, `enum_subdomains_passive` and `subfinder_status` are replaced by run-oriented tools for modules and results.
- **Out of scope:** agent swarms (start, prompt and wait on agents), Packer-prebaked images, fleet leases and sharing, Temporal External Storage and Workflow Streams, moving the control plane off the laptop.

## Capabilities

### New Capabilities

- `fleet-lifecycle`: Provisioning and destroying DigitalOcean fleets through Pulumi. Covers tailnet join with per-fleet ephemeral tagged keys, the no-inbound firewall, the readiness gate (`hc` connected, setup finished, S3 probe written on every machine), registering machines with the operator's Herdr, explicit and run-scoped lifetimes, maximum lifetime, and orphan sweeping.
- `job-execution`: Running one module invocation on one fleet machine in a Herdr (`hc`) terminal through `warden-run`, shown as a tab in a Warden space on that machine, without touching the operator's own terminals or spaces. Covers the presigned-URL job contract, the completion marker in S3, resume and re-attach after control-plane restarts, timeouts and cancellation.
- `recon-runs`: Running a catalog module across a fleet. Covers the module catalog, staging inputs to S3, deterministic sharding and placement, fan-out with retries, merging and deduplicating results, and run status. Also covers the MCP tools that start and inspect runs.
- `result-store`: The S3 data plane on the control plane. Covers bucket and key layout for runs, presigning for droplets, merged Parquet results, and read-only querying of results through DuckDB, both directly and through MCP.

### Modified Capabilities

None. No specs exist in `openspec/specs/` yet.

## Impact

- **Code removed:** `src/warden/flight_server.py`, `flight_client.py`, `flight_store.py`, `work/ssh_exec.py`, `worker.py`, `providers/docker.py`, `Dockerfile.worker`, `activities.py` (compatibility shim), plus the Flight, Docker and plan-shard tests.
- **Code reworked:**
  - `providers/digitalocean.py`: tailnet key, firewall and cloud-init.
  - `infra/workflows.py` and `infra/activities.py`: readiness, max lifetime, `hc` machine registration, sweep.
  - `work/`: `hc` driver, `run_job`, recon workflow.
  - `catalog.py`: command templates.
  - `models.py`, `client_api.py`, `mcp_server.py`, `serve.py`.
- **Dependencies:**
  - Add `pulumi-tailscale`, `boto3` and `duckdb`.
  - Remove `pulumi-docker`, `pyarrow` and the `warden-worker` script.
  - Uses the operator's existing `hc` (Herdr.app's herdr-core, 0.1.17 at planning time). `hc machine add --install` copies the same version onto each droplet, so nothing is installed by cloud-init.
- **Control-plane services:**
  - Temporal dev server on `127.0.0.1:7233`, unchanged and not exposed.
  - New SeaweedFS container on `127.0.0.1:8333`, exposed with `tailscale serve --tcp 8333`.
  - The local `hc` server drives fleet machines and also backs the operator's view in Herdr.app.
- **Configuration:**
  - New: `TAILSCALE_OAUTH_CLIENT_ID` and `TAILSCALE_OAUTH_CLIENT_SECRET` (already present), the tailnet MagicDNS suffix, the control-plane MagicDNS name, and S3 settings.
  - Removed: Flight, Docker and worker settings.
  - The operator adds a one-time SSH client stanza for fleet hostnames.
- **External systems:**
  - The tailnet policy (applied separately) defines `tag:warden-worker`.
  - The DigitalOcean account gains firewall resources.
  - The OAuth client currently has broader scope than Warden needs (it requires only `auth_keys` and `devices:core`).
