# fleet-lifecycle Specification

## Purpose
Provision, expose to the operator, and reliably tear down fleets of DigitalOcean machines that Warden runs work on. Machines are reachable only over the private tailnet, and no fleet can outlive its allowed lifetime.

## Requirements

### Requirement: Fleet provisioning
The system SHALL provision a fleet of the requested number of DigitalOcean machines, named `wf-<fleet_id>-NN` (two-digit, 1-based). Every machine SHALL carry the fleet's identifying tags.

#### Scenario: Start a two-machine fleet
- **WHEN** the operator starts a fleet with `instances=2`
- **THEN** the fleet reports state `PROVISIONING`
- **AND** two machines named `wf-<fleet_id>-01` and `wf-<fleet_id>-02` are created, tagged with `warden` and `fleet:<fleet_id>`

#### Scenario: Duplicate start is a no-op
- **WHEN** the operator starts a fleet whose `fleet_id` already has a running fleet
- **THEN** no new machines are created and the existing fleet's handle is returned

### Requirement: Private tailnet membership
Each fleet SHALL obtain its own ephemeral, preauthorized tailnet join credential, tagged `tag:warden-worker` and valid for at most one hour. Every machine SHALL join the tailnet with it, using its machine name as hostname and with Tailscale SSH enabled.

#### Scenario: Machines appear on the tailnet
- **WHEN** a fleet finishes provisioning
- **THEN** each machine appears as a tailnet device named after it, tagged `tag:warden-worker`, with Tailscale SSH enabled

#### Scenario: Credentials are not shared between fleets
- **WHEN** two fleets are provisioned
- **THEN** each fleet's machines joined with a different join credential

### Requirement: No public inbound exposure
Fleet machines SHALL accept no inbound connections from the public internet on any port. All operator and control-plane access SHALL go through the tailnet.

#### Scenario: Public port scan finds nothing
- **WHEN** any TCP port on a fleet machine's public IPv4 address is probed from the internet
- **THEN** the connection is refused or dropped

### Requirement: Readiness gate
A fleet SHALL report `READY` only once every machine is online on the tailnet, is connected to the control plane's Herdr (`hc`), has finished its boot-time setup, has the control plane's current job wrapper installed, and has written a probe object to the result store. Each machine's `hc` version SHALL be recorded.

#### Scenario: All machines ready
- **WHEN** every machine is online on the tailnet, connected in `hc`, done with boot-time setup, and has written its probe object
- **THEN** the fleet state becomes `READY`

#### Scenario: Result store unreachable from a machine
- **WHEN** a machine cannot write its probe object within the readiness timeout
- **THEN** the fleet fails with an error naming that machine and the store endpoint
- **AND** the fleet is destroyed

#### Scenario: hc version differs from the control plane
- **WHEN** a connected machine runs a different `hc` version from the control plane
- **THEN** the fleet still becomes `READY`, and the machine's version is reported in fleet status and logged

#### Scenario: Readiness timeout
- **WHEN** not every machine is ready within the readiness timeout (default 15 minutes)
- **THEN** the fleet fails with an error listing the machines that are not ready
- **AND** the fleet is destroyed

### Requirement: Operator visibility in Herdr
When a fleet becomes `READY`, every machine SHALL be registered with the operator's Herdr (`hc`) under its machine name, so it is listed among the operator's machines in Herdr.app. When the fleet is destroyed, those registrations SHALL be removed.

#### Scenario: Machines appear in Herdr
- **WHEN** a fleet becomes `READY`
- **THEN** `hc machine ls` shows one connected machine per fleet machine, named `wf-<fleet_id>-NN`, and Herdr.app's machines list shows them

#### Scenario: Machines disappear on destroy
- **WHEN** a fleet is destroyed
- **THEN** none of its machines remain registered in the operator's Herdr

### Requirement: Explicit fleet lifetime
A fleet started explicitly by the operator SHALL remain until the operator requests its destruction or its maximum lifetime elapses, whichever comes first.

#### Scenario: Operator destroys a fleet
- **WHEN** the operator requests destruction of a `READY` fleet
- **THEN** the fleet moves to `DESTROYING` and then `DONE`, and none of its resources remain

### Requirement: Run-scoped fleet lifetime
A fleet created for a single run SHALL be destroyed when that run completes, fails or is cancelled. This includes the run being terminated without a chance to clean up.

#### Scenario: Run finishes
- **WHEN** a run that created its own fleet reaches a terminal state
- **THEN** that fleet is destroyed

#### Scenario: Run is terminated
- **WHEN** a run that created its own fleet is terminated externally
- **THEN** that fleet is still destroyed

### Requirement: Maximum fleet lifetime
Every fleet SHALL have a maximum lifetime (default 2 hours, configurable per fleet). When it elapses, the fleet SHALL be destroyed whatever work is in progress. The deadline SHALL be visible in fleet status.

#### Scenario: Lifetime elapses
- **WHEN** a fleet's maximum lifetime elapses while it is `READY`
- **THEN** the fleet is destroyed and its result records that it was destroyed by the lifetime limit

#### Scenario: Control plane was offline at the deadline
- **WHEN** the control plane comes back online after a fleet's deadline has passed
- **THEN** the fleet is destroyed promptly

### Requirement: Complete and idempotent teardown
Destroying a fleet SHALL remove its machines, its firewall, its join credential, its tailnet devices, its Herdr spaces and its Herdr registrations. Destroying an already-destroyed or never-provisioned fleet SHALL succeed without error.

#### Scenario: Nothing left behind
- **WHEN** a fleet finishes destroying
- **THEN** no DigitalOcean droplet or tailnet device tagged for that fleet exists

#### Scenario: Repeated destroy
- **WHEN** destruction is requested for a fleet that is already `DONE`
- **THEN** the request succeeds and changes nothing

### Requirement: Orphan sweep at startup
When the control plane starts, it SHALL find DigitalOcean machines tagged `warden`, tailnet devices tagged `tag:warden-worker`, and Herdr machines named `wf-*` that belong to no running fleet. It SHALL destroy or unregister them and report what it removed.

#### Scenario: Orphaned droplets are removed
- **WHEN** the control plane starts and finds droplets tagged `fleet:<id>` with no running fleet for `<id>`
- **THEN** those droplets and their tailnet devices are destroyed and the sweep is logged

#### Scenario: Running fleets are untouched
- **WHEN** the control plane starts while a fleet is running
- **THEN** that fleet's machines are not swept

### Requirement: Fleet status
The system SHALL report, for any fleet: its state, its deadline, and per machine the name, tailnet hostname, public IPv4 and `hc` version. Failed fleets SHALL also report the failure reason.

#### Scenario: Status of a ready fleet
- **WHEN** the operator queries a `READY` fleet
- **THEN** the response includes state `READY`, the deadline, and one entry per machine with name, tailnet hostname, public IPv4 and `hc` version

### Requirement: Fail fast on missing credentials
If the DigitalOcean token or the Tailscale OAuth client credentials are missing, starting a fleet SHALL fail immediately, without retries and without creating any resource.

#### Scenario: Missing Tailscale credentials
- **WHEN** a fleet is started without Tailscale OAuth client credentials configured
- **THEN** the fleet fails with an error naming the missing setting and no droplet is created
