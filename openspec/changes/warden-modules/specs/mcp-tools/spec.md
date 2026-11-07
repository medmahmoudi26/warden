# Spec Delta

## Purpose

The agent-facing MCP surface of Warden. It is a small, hand-written set of tools for fleets, modules and Temporal-backed runs. Raw `hc` remains the way to do anything the tools don't cover.

## ADDED Requirements

### Requirement: Prefixed, hand-written tool set
Every MCP tool name SHALL start with the prefix of what it wraps:
- `fleet_` for fleet lifecycle;
- `module_` for modules;
- `temporal_` for anything that uses the Temporal client on a module's behalf.

Tools SHALL be written explicitly; none are generated from module contents.

#### Scenario: Tool listing
- **WHEN** an MCP client lists Warden's tools
- **THEN** it sees exactly `fleet_up`, `fleet_list`, `fleet_status`, `fleet_down`, `module_list`, `module_activate`, `module_deactivate`, `temporal_subdomain_enumeration`, `temporal_status`, `temporal_wait`, `temporal_result` and `temporal_cancel`

#### Scenario: Adding a module adds no tool
- **WHEN** a new module folder is added and `warden-serve` restarts
- **THEN** the tool list is unchanged until a tool for it is written

### Requirement: Start a fleet
`fleet_up` SHALL accept an instance count, an optional region, an optional maximum lifetime and an optional list of modules. It SHALL return the new fleet's id at once, with state `PROVISIONING`, without waiting for readiness.

#### Scenario: Start a fleet with a module
- **WHEN** an agent calls `fleet_up(instances=2, modules=["subdomains"])`
- **THEN** the call returns a fleet id with state `PROVISIONING` within seconds

### Requirement: List fleets
`fleet_list` SHALL report every fleet that is not yet `DONE`, with its id, state, deadline and machine count.

#### Scenario: Two fleets up
- **WHEN** two fleets are running and an agent calls `fleet_list`
- **THEN** both are listed with their states and deadlines

### Requirement: Fleet status for agents
`fleet_status` SHALL report the fleet's state, deadline and any failure reason. For each machine it SHALL report:
- name, tailnet hostname, public IPv4 and `hc` version;
- the Herdr space label for raw terminals;
- each module's slots, and whether each slot is running or exited (with its exit code).

#### Scenario: Status of a ready fleet with a module
- **WHEN** an agent queries a `READY` fleet where `subdomains` is active
- **THEN** each machine entry lists its `herdr.space` label and `subdomains` slot 1 as running

### Requirement: Destroy a fleet
`fleet_down` SHALL request destruction of a fleet and return at once. Calling it for a fleet that is already destroying or `DONE` SHALL succeed and change nothing.

#### Scenario: Destroy
- **WHEN** an agent calls `fleet_down` on a `READY` fleet
- **THEN** the call returns at once and the fleet moves to `DESTROYING`

### Requirement: Module tools
- `module_list` SHALL report the available modules.
- `module_activate` SHALL accept a fleet, a module and an optional worker count, and return per-machine outcomes.
- `module_deactivate` SHALL accept a fleet, a module and an optional force flag.

All three follow the `fleet-modules` requirements.

#### Scenario: Activate from an agent
- **WHEN** an agent calls `module_activate(fleet_id, "subdomains", workers=2)`
- **THEN** the call returns once every machine has started both slots or reported a failure, with one outcome per machine

### Requirement: Fleet selection for Temporal tools
A `temporal_` tool that starts a run SHALL accept an optional fleet id.
- **Fleet id omitted:** it SHALL use the only `READY` fleet, or fail listing the `READY` fleets when there are none or several.
- **Fleet given but not `READY`:** it SHALL fail stating the fleet's state.

#### Scenario: One fleet ready
- **WHEN** an agent calls `temporal_subdomain_enumeration` without a fleet id while exactly one fleet is `READY`
- **THEN** the run starts on that fleet

#### Scenario: Ambiguous fleet
- **WHEN** the same call is made while two fleets are `READY`
- **THEN** it fails, listing both fleet ids, and no run starts

### Requirement: Starting a Temporal-backed run
A tool that starts a module workflow SHALL:
1. activate the module on the fleet with one worker if it isn't active;
2. wait up to 5 minutes until every running slot of the module polls `<fleet_id>.<module>`;
3. start the workflow on that queue.

It SHALL return the run id, fleet id and task queue without waiting for the run to finish.

#### Scenario: Module not yet active
- **WHEN** `temporal_subdomain_enumeration` is called on a fleet where `subdomains` is not active
- **THEN** the module is activated, the tool waits for its workers to poll, and the run starts

#### Scenario: Workers never poll
- **WHEN** a module's slots are not all polling within 5 minutes
- **THEN** the tool fails naming the slots that are not polling, and no run starts

### Requirement: Run identity and lifetime
Each run SHALL have the id `run/<fleet_id>/<module>-<suffix>`, where the suffix is random. Its execution timeout SHALL NOT extend past the fleet's deadline.

#### Scenario: Run bounded by the fleet
- **WHEN** a run is started on a fleet whose deadline is 40 minutes away
- **THEN** the run's execution timeout is at most 40 minutes

### Requirement: Run status
`temporal_status` SHALL report a run's state (`RUNNING`, `COMPLETED`, `FAILED`, `CANCELED`, `TERMINATED` or `TIMED_OUT`), its start and close times, its task queue, its number of pending activities and, if it failed, the failure message.

#### Scenario: Running run
- **WHEN** an agent queries a run with three activities in progress
- **THEN** it sees state `RUNNING` and 3 pending activities

### Requirement: Run result
`temporal_result` SHALL return a finished run's result. If the result serialises to more than 20,000 characters, the tool SHALL write it to a JSON file under the control plane's `state/results/` and return the file path, its size and a preview. A run that is not finished SHALL produce an error stating its state. A failed run SHALL produce its failure message.

#### Scenario: Small result
- **WHEN** an agent fetches the result of a completed run whose result is 2,000 characters
- **THEN** the result is returned inline

#### Scenario: Large result
- **WHEN** a completed run's result is 300,000 characters
- **THEN** the tool returns a path under `state/results/`, the size and a preview, and the file holds the full result

### Requirement: Wait for a run
`temporal_wait` SHALL block until the run finishes or a timeout elapses (default 600 seconds, at most 3600). When the run finishes in time it SHALL return what `temporal_result` returns. Otherwise it SHALL return what `temporal_status` returns.

#### Scenario: Run finishes while waiting
- **WHEN** an agent waits on a run that completes after 30 seconds
- **THEN** the call returns the run's result after about 30 seconds

### Requirement: Cancel a run
`temporal_cancel` SHALL request cancellation of a run and return its status. Cancelling a finished run SHALL succeed and change nothing.

#### Scenario: Cancel a running run
- **WHEN** an agent cancels a running subdomain enumeration
- **THEN** its in-progress tool processes are stopped and the run ends `CANCELED`

### Requirement: Temporal tools act only on runs
`temporal_status`, `temporal_wait`, `temporal_result` and `temporal_cancel` SHALL only accept run ids starting with `run/`. Warden's own fleet and sweep workflows SHALL NOT be reachable through them.

#### Scenario: Cancel a fleet workflow
- **WHEN** an agent calls `temporal_cancel("fleet/abcd1234")`
- **THEN** the call fails, stating that it accepts only run ids, and the fleet is unaffected

### Requirement: Raw terminal path
Agents SHALL be able to run any command on a fleet machine with `hc`, outside modules. A terminal started on a fleet machine with the space label from `fleet_status` SHALL appear as a tab in that machine's Warden space, and SHALL end when the fleet is destroyed.

#### Scenario: Raw command beside modules
- **WHEN** an agent runs `hc run --on wf-<fleet_id>-01 --bg --label herdr.space=<id> -- nmap ...`
- **THEN** the command runs in a new tab of that machine's `warden wf-<fleet_id>-01` space in Herdr.app
