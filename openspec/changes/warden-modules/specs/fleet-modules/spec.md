# Spec Delta

## Purpose

Run self-contained modules on a fleet. A module is a folder with an `entrypoint.sh`. Warden copies it to every machine and runs it in visible Herdr terminals, without caring what it runs.

## ADDED Requirements

### Requirement: Module definition
A module SHALL be a folder directly under the modules directory (default `modules/`, overridable by configuration). The folder name is the module name and SHALL match `^[a-z][a-z0-9-]{0,23}$`. The folder SHALL contain an executable `entrypoint.sh`. Folders whose names start with `_` or `.`, and folders without `entrypoint.sh`, are not modules.

#### Scenario: Listing modules
- **WHEN** the operator lists modules
- **THEN** each module is reported with its name and a description taken from the first comment line of its `entrypoint.sh` (after any shebang), or an empty description if there is none

#### Scenario: Folder without an entrypoint
- **WHEN** a folder under the modules directory has no `entrypoint.sh`
- **THEN** it is not listed and cannot be activated

### Requirement: Activation preconditions
Activating a module SHALL require a `READY` fleet and a known module. Otherwise activation SHALL fail before any machine is touched.

#### Scenario: Unknown module
- **WHEN** the operator activates a module name that is not a module
- **THEN** activation fails with an error listing the available modules, and nothing is copied or started

#### Scenario: Fleet not ready
- **WHEN** the operator activates a module on a fleet that is not `READY`
- **THEN** activation fails with an error stating the fleet's state

### Requirement: Module delivery
Activation SHALL copy the module folder to `/opt/warden/modules/<module>` on every machine of the fleet, replacing any previous copy. Virtual environments, bytecode caches and version-control folders SHALL be left out.

#### Scenario: Module copied to every machine
- **WHEN** a module is activated on a two-machine fleet
- **THEN** both machines have `/opt/warden/modules/<module>/entrypoint.sh`, identical to the control plane's copy

### Requirement: Visible worker terminals
For each machine and each worker slot `n` (1 to the requested worker count, default 1), activation SHALL run `entrypoint.sh` from the module's folder in an `hc` terminal named `<module>-<n>`, labelled `warden.fleet=<fleet_id>` and `warden.module=<module>`. Herdr.app SHALL show it as a tab named `<module> #<n>` in that machine's `warden <machine>` space.

#### Scenario: Operator watches a module
- **WHEN** a module is activated with two workers on machine `wf-<fleet_id>-01`
- **THEN** that machine's `warden wf-<fleet_id>-01` space shows tabs `<module> #1` and `<module> #2` with the entrypoints' live output

#### Scenario: Herdr window unavailable
- **WHEN** Herdr.app is not running during activation
- **THEN** the entrypoints still run, activation still succeeds, and the terminals remain reachable through `hc`

### Requirement: Entrypoint environment
Each entrypoint SHALL start with these environment variables:
- `WARDEN_FLEET`, `WARDEN_MACHINE`, `WARDEN_MODULE`;
- `WARDEN_WORKER` (the slot number) and `WARDEN_WORKER_ID` (`<machine>/<module>-<n>`);
- `WARDEN_TASK_QUEUE` (`<fleet_id>.<module>`);
- `TEMPORAL_ADDRESS` (the control plane's Temporal address as reachable over the tailnet).

#### Scenario: Entrypoint reads its context
- **WHEN** the entrypoint of module `subdomains`, slot 1, starts on `wf-abcd1234-02`
- **THEN** it sees `WARDEN_TASK_QUEUE=abcd1234.subdomains` and `WARDEN_WORKER_ID=wf-abcd1234-02/subdomains-1`

### Requirement: Idempotent activation
Activating a module that is already active SHALL NOT start a second process for any slot that has a live terminal. Activating with a higher worker count SHALL start only the missing slots.

#### Scenario: Activate twice
- **WHEN** a module is activated twice with the same worker count
- **THEN** each machine still has exactly one live terminal per slot, and the second activation reports the module as already active

#### Scenario: Scale up
- **WHEN** a module active with one worker is activated with three workers
- **THEN** slots 2 and 3 are started on every machine, and slot 1 keeps running untouched

### Requirement: Partial activation failure
If a machine cannot receive the module or start its terminals, activation SHALL report which machines failed and why. Machines that succeeded SHALL stay active.

#### Scenario: One machine fails
- **WHEN** copying the module to `wf-<fleet_id>-02` fails during activation
- **THEN** activation reports `wf-<fleet_id>-02` and the error, and the module keeps running on the other machines

### Requirement: Entrypoint exit stays visible
When an entrypoint exits, for any reason, its terminal SHALL stay open, showing the output, a line naming the module and its exit code, and a shell on that machine. Warden SHALL NOT restart it automatically.

#### Scenario: Entrypoint crashes
- **WHEN** an entrypoint exits with code 1
- **THEN** its tab still shows its output followed by a line reporting exit code 1, then a shell prompt
- **AND** the module reports that slot as exited, not running

### Requirement: Module state comes from the machines
Which modules are active on a fleet, and which slots are running or exited, SHALL be determined from the terminals live on the fleet's machines, not from records kept by the control plane.

#### Scenario: Control plane restarted
- **WHEN** `warden-serve` restarts while a module is active
- **THEN** fleet status still reports the module and its running slots

### Requirement: Module deactivation
Deactivating a module on a fleet SHALL interrupt each of its worker terminals, wait up to 30 seconds for them to exit, end any that are still running, and remove the terminals.

#### Scenario: Deactivate
- **WHEN** the operator deactivates an active module
- **THEN** none of its terminals remain on any machine of the fleet, and its tabs are gone from Herdr.app

### Requirement: Deactivation protects in-flight runs
Deactivation SHALL be refused while runs started by Warden for that module on that fleet are still running, unless forced. A forced deactivation SHALL cancel those runs first.

#### Scenario: Run in flight
- **WHEN** the operator deactivates a module while one of its runs on that fleet is running, without forcing
- **THEN** deactivation fails naming the running run, and the module's terminals keep running

#### Scenario: Forced deactivation
- **WHEN** the operator forces deactivation while a run is in flight
- **THEN** the run is cancelled and then the module's terminals are stopped

### Requirement: Activation at fleet start
A fleet started with a list of modules SHALL activate them, one worker each, once it becomes `READY`. An activation failure SHALL be reported in fleet status and SHALL NOT fail or destroy the fleet.

#### Scenario: Fleet started with a module
- **WHEN** a fleet is started with `modules=["subdomains"]`
- **THEN** once it is `READY`, every machine runs a `subdomains-1` terminal

#### Scenario: Module fails at fleet start
- **WHEN** a module listed at fleet start fails to activate
- **THEN** the fleet stays `READY` and its status reports the module and the error

### Requirement: Module operations never touch the operator's terminals
Module operations SHALL only start, interrupt, end or remove `hc` terminals that are labelled `warden.module` and live on Warden fleet machines (`wf-<fleet_id>-NN`). Other terminals, including raw terminals that agents or the operator start in a Warden space, SHALL never be modified by module operations.

#### Scenario: Raw terminal beside a module
- **WHEN** a module is deactivated while an unlabelled terminal started by an agent runs in the same space
- **THEN** that terminal keeps running
