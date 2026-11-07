# Spec Delta

## Purpose

Run a catalog recon module across a fleet, axiom-style: stage the inputs, split them across machines, run one job per shard, and merge the outputs into a single deduplicated result that can be queried and inspected while running and afterwards.

## ADDED Requirements

### Requirement: Module catalog
The system SHALL provide a catalog of modules. Each module is defined by a command template with `{in}` and `{out}` placeholders and a timeout. The catalog SHALL include `subfinder` (passive subdomain enumeration) and SHALL be listable.

#### Scenario: List modules
- **WHEN** the operator lists modules
- **THEN** the response includes `subfinder` with its description and timeout

#### Scenario: Unknown module
- **WHEN** a run is requested for a module not in the catalog
- **THEN** the request fails with an error listing the known module names, and nothing is started

### Requirement: Run input staging
A run SHALL accept either an inline list of targets or a previous run's merged result as input. Before any job starts, inputs SHALL be normalized (trimmed, blank lines dropped, deduplicated, order kept) and stored in the result store.

#### Scenario: Inline targets
- **WHEN** a run is started with targets `[" example.com", "example.com", "", "test.com"]`
- **THEN** the staged input contains exactly `example.com` and `test.com`, in that order

#### Scenario: Chained input
- **WHEN** a run is started with `input_run_id` naming a finished run
- **THEN** the staged input is that run's merged result

### Requirement: Fleet selection
A run SHALL use an existing fleet when given one that is `READY`. Otherwise it SHALL create its own run-scoped fleet with the requested number of machines.

#### Scenario: Existing fleet
- **WHEN** a run is started with the `fleet_id` of a `READY` fleet
- **THEN** its jobs run on that fleet's machines and the fleet is not destroyed when the run ends

#### Scenario: Fleet not ready
- **WHEN** a run is started with the `fleet_id` of a fleet that is not `READY`
- **THEN** the request fails with an error giving the fleet's current state

#### Scenario: Run-scoped fleet
- **WHEN** a run is started with `instances=3` and no `fleet_id`
- **THEN** a fleet of 3 machines is created for the run and destroyed when the run ends

### Requirement: Deterministic sharding and placement
Inputs SHALL be split into contiguous shards that cover every input line exactly once. There SHALL be no more shards than input lines, and by default one shard per machine. Shard `i` SHALL be assigned to machine `i mod N`.

#### Scenario: Even split
- **WHEN** 10 inputs are split for a fleet of 3 machines
- **THEN** shards of 4, 3 and 3 lines are created and assigned to machines 01, 02 and 03

#### Scenario: Fewer inputs than machines
- **WHEN** 2 inputs are run on a fleet of 3 machines
- **THEN** exactly 2 shards of 1 line each are created

### Requirement: Concurrent fan-out with retries
Shards SHALL run concurrently, with at most one job per machine at a time. A failed job SHALL be retried up to the configured attempt limit (default 2 attempts in total) before its shard is marked failed.

#### Scenario: Transient failure
- **WHEN** a shard's first attempt fails and its second succeeds
- **THEN** the shard is reported as succeeded with 2 attempts

### Requirement: Result merge
Once every shard has finished, the outputs of successful shards SHALL be concatenated, normalized and deduplicated into the run's merged result. The merged row count SHALL be recorded.

#### Scenario: Overlapping shard outputs
- **WHEN** two shards both output `a.example.com`
- **THEN** the merged result contains `a.example.com` once

### Requirement: Run outcome
A run SHALL end `DONE` when at least one shard succeeded, listing any failed shards. It SHALL end `FAILED` when every shard failed. An empty input SHALL end `DONE` with zero rows without launching any job.

#### Scenario: Partial success
- **WHEN** 2 of 3 shards succeed
- **THEN** the run is `DONE`, its merged result holds the 2 shards' outputs, and the failed shard is listed with its reason

#### Scenario: Empty input
- **WHEN** a run's normalized input is empty
- **THEN** the run is `DONE` with zero rows and no job is launched

### Requirement: Run status
The system SHALL report, for any run: its state, its fleet, its shard counts (total, running, succeeded, failed), per shard the machine, attempts, exit code and output count, and the merged result's location and row count.

#### Scenario: Status while running
- **WHEN** the operator queries a run with 3 shards, one finished
- **THEN** the response shows state `RUNNING`, 1 succeeded, 2 running, and the machine of each shard

### Requirement: Operator tools for runs
The operator-facing MCP interface SHALL offer tools to: list modules, start a module run, get a run's status, wait for a run to finish, cancel a run, start a fleet, get a fleet's status, and destroy a fleet.

#### Scenario: End-to-end through MCP
- **WHEN** the operator starts a `subfinder` run with inline targets and `instances=1` through MCP, then waits for it
- **THEN** the wait returns a terminal state, the merged row count and the result location
