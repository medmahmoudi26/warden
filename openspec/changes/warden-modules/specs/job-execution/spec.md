# Spec Delta

## REMOVED Requirements

### Requirement: Jobs run in visible Herdr terminals
**Reason**: Warden no longer launches per-job terminals. Work runs inside module worker terminals, which are visible as tabs in the machine's Warden space.
**Migration**: See `fleet-modules` ("Visible worker terminals"). Tool output for subdomain enumeration streams into the worker's tab (`subdomain-enumeration`, "Visible tool output").

### Requirement: Operator's own terminals and machines are never touched
**Reason**: The guarantee stays, but it moves to the capabilities that now mutate terminals and machines.
**Migration**: See `fleet-modules` ("Module operations never touch the operator's terminals") and `fleet-lifecycle` ("Operator's machines and spaces are never touched").

### Requirement: Operator's spaces are never touched
**Reason**: The guarantee stays, but spaces are now created per machine by the fleet.
**Migration**: See `fleet-lifecycle` ("Operator's machines and spaces are never touched").

### Requirement: Job I/O through scoped, time-limited URLs
**Reason**: The result store is removed. Inputs and outputs travel as Temporal activity arguments and results between workers on the fleet and Temporal on the control plane.
**Migration**: None needed for callers. Modules that need large outputs bring their own storage.

### Requirement: Completion marker is the source of truth
**Reason**: Activity completion recorded by Temporal replaces the S3 completion marker.
**Migration**: None. Run state is read with `temporal_status`.

### Requirement: Failed job reporting
**Reason**: Per-job results are replaced by per-chunk outcomes inside a module's run result.
**Migration**: See `subdomain-enumeration` ("Run result"), which reports failed chunks with their last error.

### Requirement: Resume without duplicate execution
**Reason**: Temporal dispatches each activity attempt to exactly one polling worker, and the workflow lives in Temporal's history. A control-plane restart therefore neither duplicates nor loses work, and Warden needs no re-attach logic.
**Migration**: None.

### Requirement: Lost job detection
**Reason**: Activity heartbeat timeouts detect lost workers.
**Migration**: See `subdomain-enumeration` ("Chunk retries and timeout").

### Requirement: Job timeout
**Reason**: Activity start-to-close timeouts replace the job wrapper's timeout.
**Migration**: See `subdomain-enumeration` ("Chunk retries and timeout").

### Requirement: Job cancellation
**Reason**: Temporal cancellation, delivered to activities through heartbeats, replaces interrupting job terminals.
**Migration**: See `mcp-tools` ("Cancel a run") and `subdomain-enumeration` ("Cancellation stops tools").

### Requirement: Terminal retention
**Reason**: There are no per-job terminals left to retain. Module terminals stay open after their entrypoint exits, and every terminal on a fleet ends when its spaces are closed at teardown.
**Migration**: See `fleet-modules` ("Entrypoint exit stays visible") and `fleet-lifecycle` ("Complete and idempotent teardown").
