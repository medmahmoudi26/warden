# Spec Delta

## MODIFIED Requirements

### Requirement: Prefixed, hand-written tool set
Every MCP tool name SHALL start with the prefix of what it wraps:
- `fleet_` for fleet lifecycle;
- `module_` for modules;
- `temporal_` for anything that uses the Temporal client on a module's behalf.

Tools SHALL be written explicitly; none are generated from module contents.

#### Scenario: Tool listing
- **WHEN** an MCP client lists Warden's tools
- **THEN** it sees exactly these 16 tools:
  - `fleet_up`, `fleet_list`, `fleet_status`, `fleet_down`;
  - `module_list`, `module_activate`, `module_deactivate`;
  - `temporal_subdomain_enumeration`, `temporal_web_probe`, `temporal_nuclei_scan`, `temporal_web_fuzzing`, `temporal_desync_scan`;
  - `temporal_status`, `temporal_wait`, `temporal_result`, `temporal_cancel`.

#### Scenario: Adding a module adds no tool
- **WHEN** a new module folder is added and `warden-serve` restarts
- **THEN** the tool list is unchanged until a tool for it is written
