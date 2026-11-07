# Design

## Context

`warden-modules` (implemented, not yet archived) made modules self-contained folders: an `entrypoint.sh` started in Herdr tabs on every fleet machine, Temporal workers polling `<fleet_id>.<module>`, and hand-written `temporal_` MCP tools that start workflows by name. Results travel back as the workflow's return value, guarded at 1.5 MB because Temporal rejects payloads over 2 MB. That guard already bit once in the live run: one `example.com` chunk produced 2.2 MB.

The new steps produce much more than that. httpx JSON is about 1-3 KB per site, and nuclei findings carry raw requests. reconftw writes these as files (`webs/web_full_info.txt`, `nuclei_output/*.txt`, `fuzzing/*.txt`), and the operator wants the same.

Fleet machines reach the operator's laptop only on Temporal's port (`tag:warden-worker → warden-control:7233`), so Temporal is the only path back. Fleet machines are destroyed with the fleet, so files left on them are lost.

Reference for the subdomain logic: reconftw `main` at `39252113a854bdac0b785f144b9fae1fa8ca35fc`, specifically:
- `modules/subdomains.sh` (`sub_passive`, `sub_active`, `sub_brute`, `sub_permut`, `sub_regex_permut`);
- `modules/utils.sh` (`_resolve_domains`, `_bruteforce_domains`);
- `modules/web.sh` (`webprobe_full`, `_nuclei_*`, `fuzz`);
- `reconftw.cfg`.

## Goals / Non-Goals

**Goals:**
- reconftw's passive + resolve + brute + permutation pipeline per domain, on the fleet, visible in the tabs.
- Three new modules whose output reaches the operator's disk as JSONL plus human-readable text, whatever its size within reason (hundreds of MB per run).
- Runs chain without passing host lists through the agent.

**Non-Goals:**
- Other reconftw subdomain techniques are left out:
  - ASN/CIDR, TLS and certificate pivots;
  - NOERROR, SRV and PTR checks;
  - scraping, analytics and recursion;
  - AI permutations (subwiz needs torch, about 1 GB);
  - takeover checks.
- No uncommon-port sweep by default (`ports` is optional), no screenshots.
- No nuclei DAST, headless, code or file templates. Those need explicit flags; "default templates" means without them.
- No per-host ffuf files, no recursion.
- No result upload to object storage, and no streaming of files while a run is in flight. The tabs show live output, and files appear when the result is fetched.

## Decisions

### 1. Files ride in activity results and are assembled from workflow history

**What a module returns.** An activity returns `{"warden_files": {"<name>": "<base64 of gzipped text>"}, ...summary}`. The files are gzip-compressed and base64-encoded, and the whole result stays under 1.5 MB. Text output from these tools compresses 5-10×, so one chunk can carry roughly 7-15 MB of raw text. The workflow keeps only the summaries and never returns file contents.

**How Warden collects them.** When a run's result is fetched, Warden:
1. walks the run's history (`fetch_history_events`);
2. decodes every `ActivityTaskCompleted` result with the client's data converter;
3. takes the `warden_files` entries;
4. writes the merged files atomically into `state/results/<fleet>-<module>-<hex>/`.

**Why this approach:**
- Completed activity results already live in the history on the laptop's Temporal server, and they survive fleet destruction.
- Retried attempts never appear twice, because only successful completions are recorded.
- Cancelled and failed runs still yield the chunks that finished.
- Warden still imports no module code. The contract is a JSON shape.

**Alternatives considered:**
- *Control-plane `store_output` activity, called by module workflows on `warden-orchestrator-queue`.* Files would stream to disk during the run. But every payload would sit in history twice (activity result, then the store activity's input), module workflows would be coupled to Warden's queue name, and runs would stall when `warden-serve` is down.
- *Leave files on the machine and pull them with `hc file read`.* This is lost on `fleet_down` or deadline expiry, and `hc file read` handles text only.
- *Object storage (S3/SeaweedFS).* This was just removed from the design. It would mean new credentials on the fleet.

**Limit.** History is capped at 50 MB by default, which is about 250-500 MB of raw output per run. Larger jobs split into several runs, and the chunk guard says so.

### 2. Merging rule is format-agnostic

Same-name parts are concatenated in completion order with duplicate lines dropped, and `.txt` files are then sorted.
- For `subdomains.txt` this gives `sort -u`.
- For `ffuf.txt` it matches reconftw's `sort -k1` (lines start with the status).
- For nuclei it groups findings by template.

JSONL keeps completion order. Modules choose file names, and Warden knows no module formats.

### 3. Chaining reads the local run folder

`from_run` resolves to `(module, file)` per tool:
- `temporal_web_probe` reads `subdomains` → `subdomains.txt`;
- `temporal_nuclei_scan` and `temporal_web_fuzzing` read `webs` → `urls.txt`.

The module is taken from the run id (`run/<fid>/<module>-<hex>`), and the run must be `COMPLETED`. The folder is assembled first if it is missing. Targets then go through the same normalisation and limits as explicit targets. Input stays a plain list in the workflow argument: 50,000 hosts at about 30 bytes each is about 1.5 MB, so input is also checked by serialised size.

### 4. Subdomains: a child workflow per domain, an activity per step; web modules: an activity per chunk

**Subdomains.** `SubdomainEnum` starts a `DomainEnum` child workflow per domain, at most 100 at a time. Each child runs reconftw's steps as separate activities:

    passive -> resolve -> bruteforce -> resolve -> permute -> permute -> regex_permute

reconftw's thresholds are orchestration, so they live in the workflow: which names gotator permutes, the full or short list, and when regulator runs. Each step has its own timeout:

| step | timeout |
| --- | --- |
| passive | `passive_minutes` (default 180) plus 15 minutes |
| resolve | 4 hours |
| bruteforce | 6 hours |
| each permute round | 6 hours |
| regex_permute | 4 hours |

Each step gets 3 attempts and a 2-minute heartbeat timeout. Workers send heartbeats every 10 seconds (`max_heartbeat_throttle_interval`).

**Why 2 minutes.** By default the SDK sends a heartbeat to the server only every 80% of the timeout. With the first 60-second timeout, that left about 12 seconds of slack: in the live run, a short tailnet blip timed out a finished 25-minute brute force, which Temporal then ran again. Now a step survives a link outage of up to about 110 seconds, and a lost worker is detected within 2 minutes.

**Bounding the passive step.** The `passive_minutes` option bounds subfinder. This was learned live: with a VirusTotal key, subfinder pages through example.com at the free tier's 4 requests a minute, and would use reconftw's full 180 minutes.

**Why this is durable.** A step that fails, or loses its worker, is retried alone, on any worker. Finished steps stay in the child's history and are never redone: a crash during regulator does not repeat a 25-minute brute force.

**Payload limits.** Names pass between steps as activity inputs and results, so a step's names must fit in a Temporal payload. Every payload between Warden and the fleet goes through a zlib payload codec (`src/warden/codec.py`, with a copy in each module's `helpers.py`), which shrinks hostname lists about tenfold.

The live run showed why the codec is needed: example.com's passive step returned 62,937 names, about 1.6 MB uncompressed, and failed the 2 MB limit. The guard now measures the compressed size (1.5 MB, a few hundred thousand names). A step over it fails without retry and says so.

**Trade-off of the codec.** The Temporal UI shows payloads as binary unless a codec server is configured. Payloads written before the codec existed pass through undecoded. A gotator round's candidates can reach 2 GB, so they never leave the machine: one `permute` activity generates and resolves them.

**Why child workflows.** They keep each domain's history separate. The parent holds about 6 events per domain, so a run is capped at 5,000 domains, well under the 50K-event history limit and within Temporal's 2,000 pending-children limit.

**Files.** Files come from the step activities:
- `passive` → `passive.txt`;
- every resolving step → `subdomains.txt`, which Warden merges, deduplicates and sorts.

Warden's collector follows child workflows.

**Teardown and deactivation.** Fleet teardown and module deactivation act only on top-level runs (`run/<fleet>/<module>-<id>`). Children go with their parent.

**Alternative considered.** One activity per chunk running the whole pipeline (the first implementation). It was simpler, but a late failure redid every step, and nothing showed the pipeline as Temporal code.

**Web modules.** `webs`, `nuclei` and `ffuf` are one tool run per chunk, so they stay one activity per chunk:

| module | default chunk | timeout | pacing |
| --- | --- | --- | --- |
| httpx | 200 | 60 minutes | 150 rps |
| nuclei | 25 | 4 hours | 150 rps |
| ffuf | 5 | 2 hours | URLs one after another, `-maxtime 900` each |

ffuf fuzzes `<url>/FUZZ` with leaky-paths' leading slashes removed. Its auto-calibration probes (`admin<random>`, `.htaccess<random>`, …) have no leading slash, so with `<url>FUZZ` they land on another host name, fail, and filter nothing. That was seen live: catch-all hosts returned all 1,998 entries. Even with calibration, a host can return every entry, for example when its not-found page varies. Chunks of 5 URLs keep that worst case (about 10k lines) far under the payload guard.

### 5. Tools stream through the activity, which prints readable lines

httpx, nuclei and ffuf write JSON lines to stdout, which the activity reads as a pipe. For each line the activity:
1. appends it to the chunk's JSONL;
2. prints the human-readable form to the tab;
3. keeps the line for the human-readable file.

The tool's stderr (progress, banners, stats) goes straight to the terminal. The human-readable formats are reconftw's jq formats. DNS tools (puredns, gotator, regulator, subfinder) write to files, with their output inherited by the terminal.

### 6. Self-contained modules in the plain Temporal layout

Every module folder has the same five files:

| File | Contents |
| --- | --- |
| `entrypoint.sh` | its pinned installs, then `exec uv run --script worker.py` |
| `worker.py` | the Temporal worker: one activity at a time, identity `WARDEN_WORKER_ID`, stops on SIGINT/SIGTERM |
| `workflows.py` | workflow code only: the workflow classes, their inputs, and their timeouts and retry policies. Activities and helpers are imported through `workflow.unsafe.imports_passed_through()`, as the SDK recommends. |
| `activities.py` | activity code only: the `@activity.defn` functions, their inputs and results, and their tool flags |
| `helpers.py` | every other function: the tool runner (process group, heartbeats, kill on cancel), file packing, output formatting, scope filtering, and the workflow's cancellation and error helpers |

**Why no sharing.** Each module's `helpers.py` repeats the few generic helpers (tool runner, kill, packing), so each folder reads and runs on its own. That is the operator's choice over a shared library.

**Test loading.** Every module's files have the same names, as the worker imports them. The tests therefore load one module at a time: they put its folder first on `sys.path` and re-import `workflows`, `activities` and `helpers`. The Temporal sandbox re-imports `workflows` by name from that same folder.

### 7. Installs

Every download is checked against a pinned SHA-256, or comes through a verified channel.

| Item | How |
| --- | --- |
| subfinder 2.17.0, httpx 1.12.0, nuclei 3.11.1, ffuf 2.3.0 | release archives, SHA-256 from the GitHub release digests |
| puredns 2.1.1 | release tgz, SHA-256 computed and pinned |
| massdns, Go, build-essential, git | apt (signed). Ubuntu 24.04 ships massdns. |
| gotator | `go install github.com/Josue87/gotator@85554801a28a…` (Go checksum database). Its release tag `v1.1` is not valid module semver, hence the commit. |
| regulator | `git clone` + checkout of commit `2371a060…` (the commit hash verifies the content). A uv venv with pinned `dank==0.0.23`, `datrie==0.8.3`, `editdistance==0.8.1`, `tldextract==5.4.0`, `dnspython==2.9.2`. `dank` builds without isolation against a pre-installed `pybind11`, because its `setup.py` imports it. |
| reconftw `subdomains.txt.gz`, `permutations_list*.txt`, trusted resolvers gist; n0kovo huge list; leaky-paths | raw URLs at pinned commits, SHA-256 pinned |
| trickest `resolvers.txt` | latest, refreshed when older than 24 h, rejected if it has fewer than 1,000 IPv4 lines. It is a daily-validated list whose value is freshness, and trusted-resolver validation guards correctness. |
| nuclei templates | `nuclei -update-templates` at install, so the latest. The version is recorded in each run result. |

Installs run under `flock` once per machine. Data lives in `/opt/warden/data/<module>/`, outside the module folder, which shipping replaces.

### 8. Fleet changes for heavier modules

- **Droplet size:** `FleetSpec.size` (from `fleet_up(size=…)`) overrides `WARDEN_DO_SIZE`.
- **Swap:** cloud-init adds a 2 GB swap file, so a 1 GB droplet survives nuclei's peaks instead of being OOM-killed. 2 vCPU / 4 GB is still recommended for nuclei.
- **Worker wait:** run start waits up to 15 minutes for pollers, because a first `subdomains` install builds `dank` and fetches about 90 MB of wordlists. It fails fast when a slot of the module has an exit code.
- **Run timeout:** default execution timeout = min(24 h, fleet remaining), because brute-force runs outlive the old 2-hour default.

### 9. Provider config stays out of git, but ships

`modules/*/config/provider-config.yaml` goes in `.gitignore`. `pack()` still includes it, because it ships what is on disk. The entrypoint passes `-pc` only when the file exists. The keys reach fleet machines that are root-only and destroyed with the fleet.

## Risks / Trade-offs

- **[Mass DNS and scanning from DigitalOcean can trigger abuse reports]** → reconftw's default rates are used; scope is the operator's responsibility; the tool descriptions say these runs are active.
- **[Long runs]** Brute force with a 7.6 M wordlist takes about 25 minutes per domain, plus wildcard checks. → `bruteforce=false` / `permutations=false` for quick passes; the 6-hour chunk timeout; the fleet lifetime bounds the run.
- **[Gotator explosion]** → reconftw's input thresholds and the 2 GB cap; resolution cost is bounded by puredns rates and the chunk timeout.
- **[History growth]** → compressed parts; a chunk guard with a "smaller chunk_size" message; the 50 MB default history limit is documented.
- **[First install time on a 1 vCPU machine]** About 3-5 minutes: apt toolchain, `dank` build, wordlists. → the 15-minute poll wait, fail-fast on an exited slot, and installs once per machine.
- **[massdns from apt may differ from upstream master]** puredns 2.1.1 works with massdns 1.x. → checked in a container install test and the live run.
- **[Unpinned resolvers and templates]** → both are data, not code. Resolvers are validated by trusted resolvers. The templates version is recorded per run.
- **[Conntrack table full under mass DNS]** massdns keeps one UDP flow per resolver (about 12k entries). It also retries truncated answers over TCP, at hundreds of connections a second, and each of those stays in TIME_WAIT or SYN_SENT for 120 s. The default table (65,536 entries) fills up. The kernel then drops new packets, so the worker's Temporal heartbeats fail with "dns error" and the brute force slows from 5,000 to about 1,400 queries a second. This was seen live on two large targets, where TCP accounted for 153k of 165k entries. → the subdomains entrypoint raises `nf_conntrack_max` to 262,144 and shortens the UDP timeout to 10 s, TCP TIME_WAIT to 10 s and SYN_SENT to 20 s. It never undoes a higher limit or a shorter timeout already set.
- **[Raw nuclei data]** Requests and responses can be large. → a per-chunk fallback drops raw fields only when needed, and says so.

## Migration Plan

- **Archive order:** `warden-modules` first, then this change. Its `subdomain-enumeration` and `mcp-tools` deltas modify specs that change introduces.
- **Restart:** `warden-serve` must be restarted to load the new tools.
- **Live fleets:** existing fleets keep old module code until the module is re-activated after its slots exit. Destroy them and start new ones.
- **Breaking change:** callers of `temporal_subdomain_enumeration` that read `result.subdomains` now read `subdomains.txt` from `files`.
