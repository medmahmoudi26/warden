# Spec Delta

## REMOVED Requirements

### Requirement: Module catalog
**Reason**: Command templates are replaced by module folders with an `entrypoint.sh`.
**Migration**: See `fleet-modules` ("Module definition"). The subfinder template becomes `modules/subdomains`.

### Requirement: Run input staging
**Reason**: Inputs are passed as workflow arguments; nothing is staged in a store.
**Migration**: Pass domains directly to `temporal_subdomain_enumeration`. See `subdomain-enumeration` ("Input normalisation").

### Requirement: Fleet selection
**Reason**: Runs no longer create run-scoped fleets.
**Migration**: See `mcp-tools` ("Fleet selection for Temporal tools").

### Requirement: Deterministic sharding and placement
**Reason**: Workers on the fleet poll a shared task queue, so Temporal assigns work to whichever worker is free. Warden no longer places shards on machines.
**Migration**: See `subdomain-enumeration` ("Fan-out across the fleet").

### Requirement: Concurrent fan-out with retries
**Reason**: Fan-out and retries are now part of each module's workflow.
**Migration**: See `subdomain-enumeration` ("Fan-out across the fleet", "Chunk retries and timeout").

### Requirement: Result merge
**Reason**: Each module's workflow merges its own results. There is no Parquet merge.
**Migration**: See `subdomain-enumeration` ("Run result").

### Requirement: Run outcome
**Reason**: Run outcomes are Temporal workflow outcomes, and partial results are reported in the module's result.
**Migration**: See `mcp-tools` ("Run status") and `subdomain-enumeration` ("Run result").

### Requirement: Run status
**Reason**: Replaced by the generic Temporal run status.
**Migration**: See `mcp-tools` ("Run status").

### Requirement: Operator tools for runs
**Reason**: The run tools are replaced by prefixed, hand-written tools.
**Migration**: See `mcp-tools`. `run_module` becomes the module's `temporal_` tool; `run_status`, `wait_run` and `cancel_run` become `temporal_status`, `temporal_wait` and `temporal_cancel`.
