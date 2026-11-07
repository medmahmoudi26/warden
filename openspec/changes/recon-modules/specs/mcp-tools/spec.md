# Spec Delta

## MODIFIED Requirements

### Requirement: Prefixed, hand-written tool set
Every MCP tool name SHALL start with the prefix of what it wraps:
- `fleet_` for fleet lifecycle;
- `module_` for modules;
- `temporal_` for anything that uses the Temporal client on a module's behalf.

Tools SHALL be written explicitly; none are generated from module contents.

#### Scenario: Tool listing
- **WHEN** an MCP client lists Warden's tools
- **THEN** it sees exactly these 15 tools:
  - `fleet_up`, `fleet_list`, `fleet_status`, `fleet_down`;
  - `module_list`, `module_activate`, `module_deactivate`;
  - `temporal_subdomain_enumeration`, `temporal_web_probe`, `temporal_nuclei_scan`, `temporal_web_fuzzing`;
  - `temporal_status`, `temporal_wait`, `temporal_result`, `temporal_cancel`.

#### Scenario: Adding a module adds no tool
- **WHEN** a new module folder is added and `warden-serve` restarts
- **THEN** the tool list is unchanged until a tool for it is written

### Requirement: Start a fleet
`fleet_up` SHALL accept an instance count, an optional region, an optional DigitalOcean droplet size, an optional maximum lifetime and an optional list of modules. Without a size, the configured default SHALL be used. It SHALL return the new fleet's id at once, with state `PROVISIONING`, without waiting for readiness.

#### Scenario: Start a fleet with a module
- **WHEN** an agent calls `fleet_up(instances=2, modules=["subdomains"])`
- **THEN** the call returns a fleet id with state `PROVISIONING` within seconds

#### Scenario: Larger machines
- **WHEN** an agent calls `fleet_up(size="s-2vcpu-4gb")`
- **THEN** the fleet's droplets are created with size `s-2vcpu-4gb`

### Requirement: Starting a Temporal-backed run
A tool that starts a module workflow SHALL:
1. activate the module on the fleet with one worker if it isn't active;
2. wait up to 15 minutes until every running slot of the module polls `<fleet_id>.<module>`;
3. start the workflow on that queue.

While waiting, if a slot of the module has exited, the tool SHALL fail at once, naming the slot and its exit code. It SHALL return the run id, fleet id and task queue without waiting for the run to finish.

#### Scenario: Module not yet active
- **WHEN** `temporal_subdomain_enumeration` is called on a fleet where `subdomains` is not active
- **THEN** the module is activated, the tool waits for its workers to poll, and the run starts

#### Scenario: Workers never poll
- **WHEN** a module's slots are not all polling within 15 minutes
- **THEN** the tool fails naming the slots that are not polling, and no run starts

#### Scenario: Install fails
- **WHEN** a slot's entrypoint exits with code 1 during its install, while the tool waits
- **THEN** the tool fails within seconds naming that slot and exit code, and no run starts

### Requirement: Run identity and lifetime
Each run SHALL have the id `run/<fleet_id>/<module>-<suffix>`, where the suffix is random. Its execution timeout SHALL be the fleet's remaining lifetime, capped at 24 hours.

#### Scenario: Run bounded by the fleet
- **WHEN** a run is started on a fleet whose deadline is 40 minutes away
- **THEN** the run's execution timeout is at most 40 minutes

### Requirement: Run result
`temporal_result` SHALL return a finished run's state, its result and its files (see `run-files`).
- **Large result:** if the result serialises to more than 20,000 characters, the tool SHALL write it to `result.json` in the run's folder under `state/results/`, and return that path, its size and a preview instead.
- **Run not finished:** an error stating its state.
- **Failed, cancelled, terminated or timed-out run:** its state, its failure message and the files of its completed chunks.

#### Scenario: Small result
- **WHEN** an agent fetches the result of a completed run whose result is 2,000 characters
- **THEN** the result is returned inline, with the run's files

#### Scenario: Large result
- **WHEN** a completed run's result is 300,000 characters
- **THEN** the tool returns a path under `state/results/`, the size and a preview, and the file holds the full result

### Requirement: Run status
`temporal_status` SHALL report:
- a run's state (`RUNNING`, `COMPLETED`, `FAILED`, `CANCELED`, `TERMINATED` or `TIMED_OUT`);
- its start and close times;
- its task queue;
- its numbers of pending activities and pending child workflows;
- if it failed, the failure message.

A run's child workflows SHALL be cancelled or terminated with the run. Fleet teardown and module deactivation SHALL act on runs only, never on their children directly.

#### Scenario: Running run
- **WHEN** an agent queries a run with three activities in progress
- **THEN** it sees state `RUNNING` and 3 pending activities

#### Scenario: Subdomain run in progress
- **WHEN** an agent queries a subdomain enumeration of 4 domains still in progress
- **THEN** it sees state `RUNNING` and 4 pending child workflows
