# Spec Delta

## ADDED Requirements

### Requirement: Concurrent fleets
The system SHALL run any number of fleets at once. Each fleet has its own machines, join credential, lifetime and module task queues. Destroying one fleet SHALL NOT affect another.

#### Scenario: Two fleets side by side
- **WHEN** two fleets are started one after the other
- **THEN** both reach `READY` with distinct machine names, and destroying the first leaves the second `READY` with its modules running

### Requirement: Operator's machines and spaces are never touched
The system SHALL only add or remove `hc` machines named `wf-<fleet_id>-NN`. It SHALL only create or close Herdr.app spaces that live on those machines, and only through Herdr.app's documented commands. The operator's other machines and spaces, and the app's layout document, SHALL never be modified.

#### Scenario: Operator's spaces preserved
- **WHEN** Warden creates spaces for a fleet and later destroys that fleet
- **THEN** the operator's own machines and spaces are unchanged, and only spaces on that fleet's machines were created or closed

### Requirement: Fleet readiness gate
A fleet SHALL report `READY` only once every machine:
- is online on the tailnet;
- is connected to the control plane's Herdr (`hc`);
- has finished its boot-time setup;
- can open a connection to the control plane's Temporal address over the tailnet.

Each machine's `hc` version SHALL be recorded.

#### Scenario: All machines ready
- **WHEN** every machine is online on the tailnet, connected in `hc`, done with boot-time setup, and can reach the control plane's Temporal address
- **THEN** the fleet state becomes `READY`

#### Scenario: Temporal unreachable from a machine
- **WHEN** a machine cannot connect to the control plane's Temporal address within the readiness timeout
- **THEN** the fleet fails with an error naming that machine and the Temporal address
- **AND** the fleet is destroyed

#### Scenario: hc version differs from the control plane
- **WHEN** a connected machine runs a different `hc` version from the control plane
- **THEN** the fleet still becomes `READY`, and the machine's version is reported in fleet status and logged

#### Scenario: Readiness timeout
- **WHEN** not every machine is ready within the readiness timeout (default 15 minutes)
- **THEN** the fleet fails with an error listing the machines that are not ready
- **AND** the fleet is destroyed

## MODIFIED Requirements

### Requirement: Operator visibility in Herdr
When a fleet becomes `READY`, every machine SHALL be registered with the operator's Herdr (`hc`) under its machine name, so it is listed among the operator's machines in Herdr.app. Each machine SHALL also get a `warden <machine>` space that lives on that machine and opens with a shell tab. The space is best effort. When the fleet is destroyed, the spaces and registrations SHALL be removed.

#### Scenario: Machines appear in Herdr
- **WHEN** a fleet becomes `READY`
- **THEN** `hc machine ls` shows one connected machine per fleet machine, named `wf-<fleet_id>-NN`, and Herdr.app's machines list shows them
- **AND** Herdr.app shows a `warden wf-<fleet_id>-NN` space on each machine with a shell tab

#### Scenario: Herdr window unavailable
- **WHEN** Herdr.app is not running when the fleet becomes ready
- **THEN** the fleet still becomes `READY`, and the space is created the next time a module is activated on that machine while Herdr.app runs

#### Scenario: Machines disappear on destroy
- **WHEN** a fleet is destroyed
- **THEN** none of its machines remain registered in the operator's Herdr, and no Herdr.app space remains on any of them

### Requirement: Complete and idempotent teardown
Destroying a fleet SHALL first terminate the still-running runs that Warden started on that fleet. It SHALL then remove:
- its Herdr spaces, which ends every terminal in them;
- its Herdr registrations;
- its machines, firewall and join credential;
- its tailnet devices.

Destroying an already-destroyed or never-provisioned fleet SHALL succeed without error.

#### Scenario: Nothing left behind
- **WHEN** a fleet finishes destroying
- **THEN** no DigitalOcean droplet or tailnet device tagged for that fleet exists

#### Scenario: Runs in flight at teardown
- **WHEN** a fleet is destroyed while a run started on it is running
- **THEN** the run ends `TERMINATED` with a reason naming the fleet's destruction, before the machines are destroyed

#### Scenario: Repeated destroy
- **WHEN** destruction is requested for a fleet that is already `DONE`
- **THEN** the request succeeds and changes nothing

### Requirement: Fleet status
The system SHALL report, for any fleet, its state and its deadline. For each machine it SHALL report:
- name, tailnet hostname, public IPv4 and `hc` version;
- its Herdr space label;
- each active module's slots, and whether each is running or exited.

Failed fleets SHALL also report the failure reason, and module activation errors SHALL be reported too.

#### Scenario: Status of a ready fleet
- **WHEN** the operator queries a `READY` fleet with module `subdomains` active
- **THEN** the response includes state `READY`, the deadline, and, for each machine, its name, tailnet hostname, public IPv4, `hc` version and space label, plus `subdomains` slot 1 as running

## REMOVED Requirements

### Requirement: Run-scoped fleet lifetime
**Reason**: Runs no longer create fleets. Workflows run on the fleet's own workers, so a run can't provision the machines it runs on. Agents start a fleet with `fleet_up` and destroy it with `fleet_down`. The maximum lifetime still bounds every fleet.
**Migration**: Replace `run_module(..., instances=N)` with `fleet_up(instances=N, modules=[...])`, then the module's `temporal_` tool, then `fleet_down`.

### Requirement: Readiness gate
**Reason**: The gate no longer installs a job wrapper or writes a probe object to the result store, which is removed. It now checks that each machine can reach Temporal, which module workers need. Requirements cannot drop a scenario in place, so it is replaced by "Fleet readiness gate".
**Migration**: See "Fleet readiness gate". The "Result store unreachable from a machine" scenario becomes "Temporal unreachable from a machine".
