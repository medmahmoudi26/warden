"""Fleet dataclasses and identifiers.

Everything here crosses the Temporal boundary (workflow inputs, activity
arguments, query results), so it is plain dataclasses only.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from enum import StrEnum

FLEET_ID_RE = re.compile(r"^[a-z0-9]{8}$")
MACHINE_RE = re.compile(r"^wf-([a-z0-9]{8})-\d{2,}$")
RUN_PREFIX = "run/"


class FleetState(StrEnum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    DESTROYING = "DESTROYING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass
class FleetSpec:
    fleet_id: str
    instances: int = 1
    region: str | None = None
    image: str | None = None
    size: str | None = None  # DigitalOcean droplet size; WARDEN_DO_SIZE when unset
    max_lifetime_min: int = 120
    ready_timeout_min: int = 15
    modules: list[str] = field(default_factory=list)


@dataclass
class MachineEntry:
    name: str
    tailnet_host: str = ""
    public_ip: str = ""
    hc_version: str = ""


@dataclass
class FleetStatus:
    fleet_id: str
    state: str
    deadline: str | None = None
    machines: list[MachineEntry] = field(default_factory=list)
    error: str | None = None
    destroy_reason: str | None = None
    module_errors: dict[str, str] = field(default_factory=dict)


@dataclass
class FleetResult:
    fleet_id: str
    state: str
    error: str | None = None
    destroy_reason: str | None = None


def new_fleet_id() -> str:
    return "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(8))


def validate_fleet_id(fleet_id: str) -> str:
    if not FLEET_ID_RE.match(fleet_id or ""):
        raise ValueError(f"fleet_id must be 8 lowercase letters or digits, got {fleet_id!r}")
    return fleet_id


def machine_name(fleet_id: str, index: int) -> str:
    """``wf-<fleet_id>-NN``, 1-based."""
    return f"wf-{fleet_id}-{index + 1:02d}"


def fleet_of(machine: str) -> str | None:
    """Fleet id of a ``wf-<fleet_id>-NN`` machine name, else None."""
    m = MACHINE_RE.match(machine or "")
    return m.group(1) if m else None


def fleet_workflow_id(fleet_id: str) -> str:
    return f"fleet/{fleet_id}"


def stack_name(fleet_id: str) -> str:
    return f"warden-fleet-{fleet_id}"


def fleet_run_prefix(fleet_id: str) -> str:
    """Workflow-id prefix of every run Warden starts on a fleet: ``run/<fleet_id>/``."""
    return f"{RUN_PREFIX}{fleet_id}/"
