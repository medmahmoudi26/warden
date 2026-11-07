# Spec Delta

## REMOVED Requirements

### Requirement: Private reachability
**Reason**: The S3 result store (SeaweedFS behind `tailscale serve`) is retired. Workers reach only Temporal on the control plane.
**Migration**: Stop the SeaweedFS container and remove its `tailscale serve` entry. Replace the tailnet policy grant for port 8333 with one for 7233.

### Requirement: Run key layout
**Reason**: No store, so no keys.
**Migration**: None.

### Requirement: Presigned URL limits
**Reason**: Machines no longer read or write a store.
**Migration**: None.

### Requirement: Merged result format
**Reason**: Results are returned by module workflows as JSON, not Parquet.
**Migration**: See `subdomain-enumeration` ("Run result").

### Requirement: Paginated result reads
**Reason**: Results are fetched whole. Large ones are written to a local JSON file that agents can read or search.
**Migration**: See `mcp-tools` ("Run result").

### Requirement: Read-only SQL over results
**Reason**: The DuckDB sandbox is removed. Agents can run `duckdb` or `jq` themselves over the local result file.
**Migration**: Use `temporal_result`, then query the returned file with local tools.

### Requirement: Run data retention
**Reason**: No store, so no retention policy. Run histories follow Temporal's namespace retention, and result files under `state/results/` are kept until deleted by hand.
**Migration**: None.
