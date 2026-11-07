# Spec Delta

## Purpose

The control plane's S3-compatible object store. It holds run inputs, per-job outputs, logs and completion markers, and merged results. Fleet machines can reach it only over the tailnet, and the operator can query results with read-only SQL.

## ADDED Requirements

### Requirement: Private reachability
The result store's S3 API SHALL be reachable from fleet machines only over the tailnet, at the control plane's tailnet hostname on port 8333. On the control plane host it SHALL listen only on loopback. It SHALL NOT be reachable from the public internet or the local LAN.

#### Scenario: Fleet machine reaches the store
- **WHEN** a fleet machine requests a presigned URL at the control plane's tailnet hostname, port 8333
- **THEN** the request is served

#### Scenario: LAN access refused
- **WHEN** a device on the control plane's local network connects to its LAN address on port 8333
- **THEN** the connection is refused

### Requirement: Run key layout
All of a run's data SHALL live under `runs/<run_id>/` in a single bucket:

- `input.txt` for the staged input;
- `shards/<shard_id>.in` for each shard's input;
- `shards/<shard_id>/a<attempt>.out`, `.log` and `.done.json` for each attempt's output, log and completion marker;
- `result.parquet` for the merged result.

#### Scenario: Finished run layout
- **WHEN** a single-shard run finishes successfully on its first attempt
- **THEN** `runs/<run_id>/` contains `input.txt`, `shards/shard-00.in`, `shards/shard-00/a1.out`, `shards/shard-00/a1.log`, `shards/shard-00/a1.done.json` and `result.parquet`

#### Scenario: Retried shard keeps both attempts
- **WHEN** a shard's first attempt fails and its second succeeds
- **THEN** both `shards/<shard_id>/a1.done.json` and `shards/<shard_id>/a2.done.json` exist, and only `a2.out` contributes to the merged result

### Requirement: Presigned URL limits
Presigned URLs given to fleet machines SHALL each cover one key and one HTTP method. They SHALL expire no sooner than the job's timeout plus 15 minutes, and no later than 24 hours after issue.

#### Scenario: Expired URL
- **WHEN** a presigned URL is used after it expires
- **THEN** the store rejects the request

### Requirement: Merged result format
A run's merged result SHALL be a Parquet file with one row per unique output line, in a string column named `value`.

#### Scenario: Read merged result
- **WHEN** a finished subfinder run's `result.parquet` is read
- **THEN** it has a single string column `value` containing one discovered hostname per row, with no duplicates

### Requirement: Paginated result reads
The system SHALL return a run's merged rows in pages, with `limit` (default 100, at most 10,000) and `offset` parameters, together with the total row count.

#### Scenario: First page
- **WHEN** the operator reads a run with 250 rows using `limit=100`
- **THEN** 100 rows are returned along with `total_rows=250`

### Requirement: Read-only SQL over results
The system SHALL run an operator-supplied SQL query against a table `results` holding a run's merged rows, and return at most 10,000 rows. Such queries SHALL NOT be able to read local files, reach the network, or change any stored data.

#### Scenario: Aggregate query
- **WHEN** the operator queries `SELECT count(*) FROM results WHERE value LIKE '%.dev.%'`
- **THEN** the count is returned

#### Scenario: Local file access blocked
- **WHEN** the operator's SQL tries to read a file on the control plane's filesystem
- **THEN** the query fails with an error and no file content is returned

### Requirement: Run data retention
A run's data SHALL stay in the store after its fleet is destroyed, so it can be queried and used as input to later runs.

#### Scenario: Query after fleet teardown
- **WHEN** a run's scoped fleet has been destroyed
- **THEN** the run's merged result can still be read and queried
