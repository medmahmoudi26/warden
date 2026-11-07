# Spec Delta

## Purpose

Run a single module invocation (a job) on one fleet machine as a visible Herdr terminal. Each job's input, output and completion travel through the result store, so jobs survive control-plane restarts without running twice.

## ADDED Requirements

### Requirement: Jobs run in visible Herdr terminals
Each job SHALL run in its own Herdr (`hc`) terminal on its assigned machine, named with the job id and labelled with the job id, run id, shard id and attempt. Herdr.app SHALL show it as a tab in a space named `warden <machine>` that lives on that machine. The terminal SHALL stay open after the job finishes, with its output visible.

#### Scenario: Operator watches a job
- **WHEN** a job is running on machine `wf-<fleet_id>-01`
- **THEN** Herdr.app shows a space `warden wf-<fleet_id>-01` on that machine with a tab showing the tool's live output
- **AND** the job's terminal carries the labels `warden.job`, `warden.run`, `warden.shard` and `warden.attempt`

#### Scenario: Output stays after the job
- **WHEN** a job finishes, whatever its outcome
- **THEN** its tab stays open with the full output and a closing summary line, above a shell on that machine

#### Scenario: Herdr window unavailable
- **WHEN** Herdr.app is not running, or does not accept the space command
- **THEN** the job still runs and is reported normally, and its terminal remains reachable through `hc`

### Requirement: Operator's own terminals and machines are never touched
The system SHALL only start, interrupt or remove `hc` terminals it created, identified by the `warden.job` label, and SHALL only add or remove `hc` machines named `wf-<fleet_id>-NN`. The operator's other terminals and machines SHALL never be modified.

#### Scenario: Unrelated terminal left alone
- **WHEN** a run is cancelled or cleaned up while the operator has other live `hc` terminals
- **THEN** only terminals labelled with that run's id are interrupted or removed

### Requirement: Operator's spaces are never touched
The system SHALL show jobs only through Herdr.app's documented commands. It SHALL only create and close spaces that live on Warden fleet machines, and SHALL never modify the operator's own spaces or the app's layout document.

#### Scenario: Operator's spaces preserved
- **WHEN** Warden shows jobs on a fleet and later destroys that fleet
- **THEN** the operator's spaces are unchanged, and only spaces on that fleet's machines were created or closed

### Requirement: Job I/O through scoped, time-limited URLs
A machine SHALL read job input and write job output, log and completion marker only through time-limited URLs, each scoped to one object key and one method. Fleet machines SHALL hold no result-store credentials.

#### Scenario: Job uses only presigned URLs
- **WHEN** a job is launched
- **THEN** the command started on the machine contains only URLs for that job's own input, output, log and marker keys, and no store credentials

#### Scenario: URL scoped to one key
- **WHEN** a job's output URL is used to write a different key
- **THEN** the store rejects the request

### Requirement: Completion marker is the source of truth
When a job finishes, a completion marker SHALL be written recording the exit code, output line count, start and finish times, and machine name. A job SHALL count as finished only once its marker exists, regardless of terminal state or output.

#### Scenario: Successful job
- **WHEN** the module command exits with code 0
- **THEN** the output and log are uploaded, then a marker with `rc=0` and the output line count is written
- **AND** the job result reports success with that count

### Requirement: Failed job reporting
A job whose command exits non-zero SHALL be reported as failed, with its exit code and the location of its log. Any output it produced SHALL still be uploaded.

#### Scenario: Tool exits non-zero
- **WHEN** the module command exits with code 2
- **THEN** the job result reports failure with `rc=2` and the log location

### Requirement: Resume without duplicate execution
A job is one attempt of one shard, identified as `<run_id>-<shard_id>-a<attempt>`. Executing the same job again (for example after a control-plane restart) SHALL NOT start a second process while the first is still running. It SHALL NOT relaunch a job that has already finished. A new attempt is a new job.

#### Scenario: Job already finished
- **WHEN** a job is executed again and its completion marker already exists
- **THEN** the recorded result is returned and nothing is started on the machine

#### Scenario: Job still running
- **WHEN** a job is executed again while its terminal is still running and no marker exists
- **THEN** execution re-attaches and waits for the marker, without starting a second process

### Requirement: Lost job detection
If a job's terminal is gone or has finished, and no completion marker was written within a short grace period, the attempt SHALL fail as lost. A later attempt SHALL launch the job afresh.

#### Scenario: Terminal disappeared
- **WHEN** a running job's terminal is removed before a marker is written
- **THEN** the attempt fails as lost, and the next retry starts a new terminal

#### Scenario: Process died without reporting
- **WHEN** a job's terminal shows its program has exited but no marker appears within the grace period
- **THEN** the attempt fails as lost

### Requirement: Job timeout
A job that runs longer than its module's timeout SHALL be interrupted and reported as failed due to timeout.

#### Scenario: Job exceeds its timeout
- **WHEN** a job runs past its module timeout
- **THEN** its process is interrupted and the job is reported as failed with reason `timeout`

### Requirement: Job cancellation
Cancelling a run SHALL interrupt the processes of that run's running jobs.

#### Scenario: Run cancelled mid-job
- **WHEN** a run is cancelled while a job is running
- **THEN** the job's process receives an interrupt and the job is reported as cancelled

### Requirement: Terminal retention
The terminal and tab of every job, whether it succeeded, failed, was lost or timed out, SHALL stay in Herdr until its fleet is destroyed. Destroying a fleet SHALL close the Herdr spaces on its machines.

#### Scenario: Finished jobs stay visible
- **WHEN** a run on a long-lived fleet finishes with one successful and one failed job
- **THEN** both jobs' terminals and tabs are still shown in Herdr.app

#### Scenario: Fleet destroyed
- **WHEN** a fleet is destroyed
- **THEN** no Herdr.app space on any of its machines remains
