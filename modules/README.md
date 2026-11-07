# Modules

A module is a folder here with an executable `entrypoint.sh`. Warden copies the
folder to every machine of a fleet and runs `entrypoint.sh` in one terminal per
worker slot. Each terminal is a tab named `<module> #<n>` in the machine's
`warden <machine>` space in Herdr.app. Warden does not care what the entrypoint
runs: a Temporal worker, a long-running tool, an agent loop.

```
modules/
  subdomains/          reconftw-style subdomain enumeration (SubdomainEnum, DomainEnum per domain)
  webs/                httpx web probing (WebProbe)
  nuclei/              nuclei with its default templates (NucleiScan)
  ffuf/                ffuf with leaky-paths (WebFuzz)
  desync/              HTTP desync checks on raw sockets, nothing to install (DesyncScan)

  <module>/
    entrypoint.sh      installs the module's pinned tools, then `exec uv run --script worker.py`
    worker.py          the Temporal worker (PEP 723 header declares temporalio)
    workflows.py       workflow code only: workflow classes, their inputs, timeouts and retries
    activities.py      activity code only: @activity.defn functions, their inputs, tool flags
    helpers.py         every other function: tool runner, file packing, formatting, ...
    config/            optional, e.g. subdomains/config/provider-config.yaml (git-ignored)
```

Each module is self-contained: no code is shared between folders.

## The contract

- **Name.** The folder name is the module name: `^[a-z][a-z0-9-]{0,23}$`.
  Folders starting with `_` or `.`, and folders without an executable
  `entrypoint.sh`, are not modules.
- **Description.** The first comment line of `entrypoint.sh` (after the shebang)
  is what `module_list` shows.
- **Where it runs.** `/opt/warden/modules/<module>` on each machine, as root,
  with `uv` and `curl` available. Bring your own tools: install them from
  `entrypoint.sh`, pinned and checksum-verified, and make the install
  idempotent and locked (`flock`: all slots on a machine start at once).
  Keep downloaded data (wordlists, resolvers, venvs) in
  `/opt/warden/data/<module>`: the module folder is replaced when it is shipped
  again.
- **Secrets.** Files in the folder are shipped as they are on disk, git-ignored
  ones included. `config/provider-config.yaml` holds subfinder's API keys and is
  git-ignored by `modules/*/config/provider-config.yaml`.
- **Environment.** Every entrypoint starts with:

  | Variable | Example |
  |---|---|
  | `WARDEN_FLEET` | `abcd1234` |
  | `WARDEN_MACHINE` | `wf-abcd1234-02` |
  | `WARDEN_MODULE` | `subdomains` |
  | `WARDEN_WORKER` | `1` (slot number) |
  | `WARDEN_WORKER_ID` | `wf-abcd1234-02/subdomains-1` |
  | `WARDEN_TASK_QUEUE` | `abcd1234.subdomains` |
  | `TEMPORAL_ADDRESS` | `mac.tail1234.ts.net:7233` (the control plane, over the tailnet) |

- **Temporal workers** poll `$WARDEN_TASK_QUEUE` and **must** set
  `identity=$WARDEN_WORKER_ID` (on the client and the `Worker`). The `temporal_`
  MCP tools wait until every running slot's identity is polling before they
  start a run. Use `max_concurrent_activities=1` so a tab shows one task at a
  time, and stop gracefully on SIGINT (`module_deactivate` sends ctrl+c, waits
  30 s, then kills).
- **Exit.** When the entrypoint exits, its tab stays open with
  `[warden] <module> #<n> exited rc=<code>` and a shell on the machine.
  Nothing restarts it; `module_activate` again replaces exited slots.
- **Changing code.** A module's folder is copied only when none of its slots is
  running on that machine, so old and new workflow code never share a queue.
  To roll out a change: `module_deactivate`, then `module_activate`.
- **Output files.** An activity can return files with its result:
  `{"warden_files": {"<name>": "<base64 of the gzipped text>"}, ...}`. Names match
  `^[a-z0-9][a-z0-9._-]{0,63}$` (not `result.json`). When a run's result is
  fetched, Warden walks the run's history and its child workflows' histories,
  and assembles the files of every completed activity into
  `state/results/<fleet>-<module>-<id>/`: same-name files are concatenated in
  completion order without duplicate lines, and `.txt` files are sorted. Files
  of failed or cancelled runs are assembled too.
- **Payloads.** Every activity input and result is a Temporal payload (2 MB
  limit). Warden's client and every module worker compress payloads with the
  same zlib codec (`ZlibCodec` in `helpers.py`, `src/warden/codec.py` on
  Warden's side): a module's worker must connect with `data_converter()`. Keep each under 1.5 MB (chunk the work, gzip the files) and fail
  without retry, naming the knob to turn, when it would not fit. Workflow
  results stay small: counts, not data.

## Adding a Temporal capability

1. Add `modules/<name>/` with `entrypoint.sh`, `worker.py`, `workflows.py`,
   `activities.py` and `helpers.py`, following an existing module.
2. Add one tool to `src/warden/mcp.py`, named `temporal_<what it does>`, that
   validates its input and calls
   `runs.start_run(..., module="<name>", workflow="<WorkflowName>", arg=...)`.
   Warden starts the workflow by name; it never imports module code. To chain
   runs, accept `from_run` and read the source run's file with
   `runs.run_targets(...)`.
3. Add tests under `tests/modules/` (fake tools in `fake_tools.py`).
4. Restart `warden-serve`, then reconnect the MCP client so it sees the new tool.
