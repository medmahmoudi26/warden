# Warden

Warden runs fleets of DigitalOcean machines that join your tailnet, and runs
**modules** on them in terminals you can watch in Herdr.app. A module is a folder
with an `entrypoint.sh`. The five that ship here are Temporal workers for recon
and scanning:
- `subdomains`: subdomain enumeration;
- `webs`: httpx probing;
- `nuclei`;
- `ffuf`;
- `desync`: HTTP desync checks on raw sockets.

Agents drive the fleet through a small MCP server, and use `hc` directly for
anything else.

```
 agent --MCP--> warden-serve (this laptop)                      Herdr.app (this laptop)
                 |  fleet_* / module_* / temporal_*                | "warden <machine>" space
                 |  FleetWorkflow per fleet (Pulumi, deadline)     | per fleet machine
                 v                                                 |
          Temporal 127.0.0.1:7233 <-- tailscale serve :7233 --+    |
                                                              |    |
   wf-abcd1234-01                                wf-...-02    |    |
   +----------------------------------------+    +---------+  |    |
   | tab "subdomains #1": entrypoint.sh ----+----+ worker  +--+    |
   |   -> uv run worker.py (polls           |    |  ...    |       |
   |      abcd1234.subdomains)              |    +---------+       |
   | tab "nmap" (raw hc, agent-started)     |                      |
   +----------------------------------------+                      |
          ^ hc over Tailscale SSH (machine add, run, exec) ---------+
```

- **Fleets** (`src/warden/fleet/`): one `FleetWorkflow` per fleet on the
  control plane. Pulumi creates the droplets, a firewall with no inbound rules,
  and an ephemeral tailnet key. The readiness gate waits for: tailnet, `hc`
  registration, `cloud-init` done, a Herdr space, and the machine reaching
  Temporal. A fleet is destroyed on request, at its maximum lifetime (default
  2 h), or on failure; teardown first terminates the runs started on it. A sweep
  at startup removes anything orphaned. Several fleets can run at once.
- **Modules** (`modules/`, `src/warden/modules.py`): activating a module copies
  its folder to every machine and starts `entrypoint.sh` in `hc` terminals, one
  per worker slot. Which slots run comes from the machines, never from records.
  See [`modules/README.md`](modules/README.md) for the contract.
- **Runs** (`src/warden/runs.py`): a `temporal_` tool makes sure its module is
  active, waits until every slot's worker polls `<fleet_id>.<module>`, then
  starts the module's workflow by name. Workflows and activities run on the
  fleet; Temporal spreads activities over the slots, retries them, times them
  out and cancels them.

## MCP tools

All hand-written in `src/warden/mcp.py`; the prefix says what a tool wraps.

| Tool | What it does |
|---|---|
| `fleet_up(instances=1, region?, size?, max_lifetime_minutes?, modules?)` | Start a fleet; returns at once (`PROVISIONING`). Listed modules are activated once `READY`. `size` is a droplet slug (default `WARDEN_DO_SIZE`; use `s-2vcpu-4gb` or larger for nuclei). Machines get 2 GB of swap |
| `fleet_list()` | Fleets not yet `DONE`: id, state, deadline, machine count |
| `fleet_status(fleet_id)` | State, deadline, errors; per machine: tailnet host, IP, `hc` version, Herdr space, module slots, raw `hc` command |
| `fleet_down(fleet_id)` | Destroy a fleet (terminates its runs first); returns at once |
| `module_list()` | Module folders and their descriptions |
| `module_activate(fleet_id, module, workers=1)` | Run a module on every machine; idempotent; replaces exited slots |
| `module_deactivate(fleet_id, module, force=false)` | Stop its slots; refused while its runs are in flight unless `force` |
| `temporal_subdomain_enumeration(domains, fleet_id?, bruteforce=true, permutations=true, deep=false)` | reconftw-style enumeration (module `subdomains`): a `DomainEnum` child workflow per domain, one activity per step (subfinder, puredns resolve, brute force, gotator ×2, regulator). Files: `subdomains.txt`, `passive.txt` |
| `temporal_web_probe(targets? \| from_run?, ports?, fleet_id?, chunk_size=200)` | httpx with verbose fields (module `webs`), from hosts/URLs or a subdomains run. Files: `webs.jsonl`, `webs.txt`, `urls.txt` |
| `temporal_nuclei_scan(targets? \| from_run?, fleet_id?, chunk_size=25)` | nuclei, default templates, every severity (module `nuclei`), from URLs or a web probe run. Files: `nuclei.jsonl`, `nuclei.txt`, `nuclei-<severity>.txt` |
| `temporal_web_fuzzing(targets? \| from_run?, fleet_id?, chunk_size=5)` | ffuf with leaky-paths, reconftw flags (module `ffuf`), from URLs or a web probe run. Files: `ffuf.jsonl`, `ffuf.txt` |
| `temporal_desync_scan(targets? \| from_run?, checks=[smuggle, split], sweep=false, tier=1, rate_ms=1000, fleet_id?, chunk_size=20)` | HTTP desync checks on raw sockets (module `desync`), from URLs or a web probe run. `smuggle`: CL.0 through an obfuscated `Content-Length` line, benign/attack/benign pipelined on one connection, a gadget-free control withdraws what it reproduces; the 5 lines from paid reports, or the ~3,600-member family with `sweep`. `split`: CRLF through Unicode folds in the path (`%DC%8A` against `%DC%8B`) with a `Host:` canary; 4 screen vectors, the tier's vectors where a URL reacts. URLs that fail a 3-repeat stability gate, or run out of their 10-minute budget (60 with `sweep`), are listed, not dropped. Files: `desync.jsonl`, `desync.txt` (leads), `skipped.txt` |
| `temporal_status(run_id)` | State, times, queue, pending activities and child workflows, failure |
| `temporal_wait(run_id, timeout_s=600)` | Wait for a run; what `temporal_result` returns if it finished, else its status |
| `temporal_result(run_id)` | A finished run's state, result and files (assembled under `state/results/<run>/`, listed with path, lines, size and the first lines of `.txt` files); failed or cancelled runs list what finished. Results over 20,000 characters go to `result.json` there |
| `temporal_cancel(run_id)` | Cancel a run; its tool processes are stopped |

`fleet_id` can be left out of `temporal_` tools when exactly one fleet is
`READY`. Run ids look like `run/<fleet_id>/<module>-<6 hex>`; the `temporal_`
tools accept nothing else. A run's execution timeout is the fleet's remaining
lifetime (at most 24 h); starting one waits up to 15 minutes for the module's
workers (first installs build tools and fetch wordlists) and fails at once if a
slot exits.

**Chaining.** `from_run` reads a completed run's output file:
`temporal_subdomain_enumeration` → `temporal_web_probe(from_run=…)` →
`temporal_nuclei_scan(from_run=…)`, `temporal_web_fuzzing(from_run=…)` and
`temporal_desync_scan(from_run=…)`. Host lists never pass through the agent.

**Active scanning.** Brute force, permutations, httpx, nuclei and ffuf send real
traffic (thousands of DNS queries per second, HTTP requests at reconftw's
rates) from the fleet's DigitalOcean IPs. The desync checks send deliberately
malformed requests (one connection per host per second by default; detection
probes only, no complete smuggled request) that fragile front ends may not
like. Point them only at authorised scope.

**Raw commands.** For anything a tool doesn't cover, use `hc` on a fleet
machine. With the space label from `fleet_status`, the terminal shows up as a
tab next to the module slots, and it ends when the fleet is destroyed:

```bash
hc run --on wf-abcd1234-01 --bg --label herdr.space=<id> -- nmap -sV example.com
hc exec --on wf-abcd1234-01 -- cat /etc/os-release
```

**Adding a capability** is a module folder plus one `temporal_` tool function in
`mcp.py`; see [`modules/README.md`](modules/README.md#adding-a-temporal-capability).

## Layout

```
src/warden/
  config.py        settings from the environment (.env)
  hc.py            hc driver + herdr-app driver (ownership guard: wf-* machines, warden.module terminals)
  tailscale.py     Tailscale API: devices, delete
  fleet/
    models.py      FleetSpec, FleetStatus, MachineEntry, ids
    provider.py    Pulumi DigitalOcean program and cloud-init (tailscale, uv, swap)
    activities.py  FleetActivities: provision, readiness, modules, teardown, sweep
    workflow.py    FleetWorkflow, SweepWorkflow
    client.py      start / list / status / destroy fleets
  modules.py       discover, pack, activate, state, deactivate (hc only)
  runs.py          start runs, wait for pollers, status / result / wait / cancel, run files, from_run (Temporal only)
  mcp.py           the MCP tools
  serve.py         warden-serve: startup checks, control-plane worker, MCP
modules/           one folder per module: subdomains, webs, nuclei, ffuf, desync (see modules/README.md)
tests/             unit tests with fakes, workflow tests on a throwaway Temporal dev server
openspec/          specs (what Warden must do) and the changes that built it
```

## Install

On the control-plane machine, a Mac (setup uses the Tailscale app and
Herdr.app), you need:
- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/);
- the [Temporal CLI](https://docs.temporal.io/cli) (`temporal`), for the dev server;
- the [Pulumi CLI](https://www.pulumi.com/docs/install/) (`pulumi`), used with a local file backend;
- Tailscale, Herdr.app and `hc`;
- a DigitalOcean API token and a Tailscale OAuth client.

```bash
git clone https://github.com/medmahmoudi26/warden.git && cd warden
uv sync --extra dev          # .venv with warden-serve, pytest and ruff
cp .env.example .env         # then fill it in: see Operator setup below
```

## Run

```bash
temporal server start-dev --db-filename .temporal/dev.db     # once, in its own terminal
.venv/bin/warden-serve --transport http --port 8008          # control plane + MCP
claude mcp add --transport http --scope user warden http://127.0.0.1:8008/mcp   # every project
```

`warden-serve` checks Temporal, `hc` and the Tailscale API at startup and exits
with a message saying what to fix. `--no-sweep` skips the startup orphan sweep.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Unit tests use fakes for `hc`, Herdr.app, Tailscale, Pulumi and Temporal. Tests
that need real task-queue pollers or visibility start a throwaway Temporal dev
server from the local `temporal` CLI (skipped if it is missing). The
`subdomains` module is tested with a fake `subfinder`.

## Operator setup

One-time setup of the control-plane machine. Fleet machines reach it only
over the tailnet: `hc` reaches them over Tailscale SSH, and their module workers
reach Temporal here on 7233.

### 1. Tailscale

The Tailscale app must be running and logged in. Use the app's CLI (the Homebrew
`tailscale` on PATH may be an older binary that does not talk to the app):

```bash
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
$TS status            # this machine is online
$TS ip -4             # must match hosts.warden-control in the tailnet policy
```

The tailnet policy must define:
- `tag:warden-worker`, and a `warden-control` host for this machine;
- a grant letting `tag:warden-worker` reach **only** `warden-control:7233`
  (policy tests: workers reach 7233 and cannot reach 22, 8233 or 8333);
- an `ssh` rule with `"action": "accept"` from you to `tag:warden-worker` as `root`.

### 2. Temporal on the tailnet

The dev server stays bound to `127.0.0.1`; publish its port to the tailnet (the
setting persists across restarts). Target `127.0.0.1`, not `localhost`: on this
machine `localhost:7233` can resolve to `[::1]`, where Docker Desktop's Temporal
extension listens.

```bash
$TS serve tcp:7233 tcp://127.0.0.1:7233                  # Tailscale 1.50 (this machine)
$TS serve --bg --tcp 7233 tcp://127.0.0.1:7233           # Tailscale >= 1.52
$TS serve status                                         # lists tcp://<this-host>:7233
temporal workflow list --address "$WARDEN_CONTROL_HOST:7233" --limit 1   # works over the tailnet name
```

Each fleet's readiness gate also checks that every machine can open
`$WARDEN_CONTROL_HOST:7233`, and fails naming the machine and address if not.

### 3. Herdr (`hc`)

Warden drives fleet machines through `hc`, the herdr-core CLI behind Herdr.app.
Herdr.app must be running. `hc machine add --install` copies this same `hc`
onto each fleet machine:

```bash
hc --version               # e.g. hc 0.1.18
hc server status           # JSON with "version"; the server is up
```

Warden only touches `hc` machines named `wf-*`, terminals labelled
`warden.module` on them, and spaces on them; your own terminals, machines and
spaces are never modified.

### 4. SSH client stanza

Warden never edits `~/.ssh/config`. Append this once so `hc` can reach fleet
machines over Tailscale SSH without host-key prompts (keys for fleet machines are
kept in a separate file that Warden prunes on destroy):

```
Host wf-*.tail1234.ts.net
  User root
  StrictHostKeyChecking accept-new
  UserKnownHostsFile ~/.ssh/known_hosts.warden
  LogLevel ERROR
```

Check it applies: `ssh -G wf-test0000-01.tail1234.ts.net | grep -E '^(user|userknownhostsfile) '`.

### 5. `.env`

Copy `.env.example` to `.env` and fill in: `DIGITALOCEAN_TOKEN`,
`TAILSCALE_OAUTH_CLIENT_ID` / `TAILSCALE_OAUTH_CLIENT_SECRET` (OAuth client with
`auth_keys` and `devices:core` write, tagged `tag:warden-worker`),
`WARDEN_TAILNET`, `WARDEN_TS_SUFFIX`, `WARDEN_CONTROL_HOST` (this machine's
MagicDNS name) and `PULUMI_BACKEND_URL` (an absolute `file://` path to
`state/pulumi`).
