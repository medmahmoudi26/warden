"""Warden settings, read from the process environment.

Entrypoints load ``.env`` with python-dotenv before calling :func:`load_settings`.
Settings that only some code paths need (cloud credentials) are optional here
and checked at the point of use, so unit tests and partial tooling do not need
a fully populated environment.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

ORCHESTRATOR_QUEUE = "warden-orchestrator-queue"

# Settings field -> environment variable. Used both to load values and to
# name the variable in error messages.
ENV_KEYS: dict[str, str] = {
    "temporal_host": "TEMPORAL_HOST",
    "temporal_fleet_address": "WARDEN_TEMPORAL_FLEET_ADDRESS",
    "orchestrator_queue": "WARDEN_ORCHESTRATOR_QUEUE",
    "pulumi_state_dir": "WARDEN_PULUMI_STATE",
    "modules_dir": "WARDEN_MODULES_DIR",
    "results_dir": "WARDEN_RESULTS_DIR",
    "do_token": "DIGITALOCEAN_TOKEN",
    "do_region": "WARDEN_DO_REGION",
    "do_size": "WARDEN_DO_SIZE",
    "do_image": "WARDEN_DO_IMAGE",
    "do_ssh_fingerprint": "WARDEN_DO_SSH_FINGERPRINT",
    "ssh_public_key_file": "WARDEN_SSH_PUBLIC_KEY_FILE",
    "ts_oauth_client_id": "TAILSCALE_OAUTH_CLIENT_ID",
    "ts_oauth_client_secret": "TAILSCALE_OAUTH_CLIENT_SECRET",
    "tailnet": "WARDEN_TAILNET",
    "ts_suffix": "WARDEN_TS_SUFFIX",
    "ts_tag": "WARDEN_TS_TAG",
    "control_host": "WARDEN_CONTROL_HOST",
    "hc_bin": "WARDEN_HC_BIN",
    "herdr_app_bin": "WARDEN_HERDR_APP_BIN",
    "known_hosts_file": "WARDEN_KNOWN_HOSTS_FILE",
    "fleet_max_lifetime_min": "WARDEN_FLEET_MAX_LIFETIME_MIN",
    "ready_timeout_min": "WARDEN_READY_TIMEOUT_MIN",
}

# Older names still accepted for the DigitalOcean token.
_DO_TOKEN_ALIASES = ("DIGITALOCEAN_TOKEN", "DIGITALOCEAN_ACCESS_TOKEN", "DO_TOKEN")
_PATHS = ("pulumi_state_dir", "modules_dir", "results_dir", "ssh_public_key_file", "known_hosts_file")


class ConfigError(RuntimeError):
    """A required setting is missing or has an invalid value."""


class MissingCredentialsError(Exception):
    """Cloud credentials are missing; fail fast, do not retry."""


@dataclass(frozen=True)
class Settings:
    # Temporal / Pulumi / local folders
    temporal_host: str = "127.0.0.1:7233"
    temporal_fleet_address: str | None = None
    orchestrator_queue: str = ORCHESTRATOR_QUEUE
    pulumi_state_dir: Path = Path("./state/pulumi")
    modules_dir: Path = Path("./modules")
    results_dir: Path = Path("./state/results")
    # DigitalOcean
    do_token: str | None = None
    do_region: str = "nyc3"
    do_size: str = "s-1vcpu-1gb"
    do_image: str = "ubuntu-24-04-x64"
    do_ssh_fingerprint: str | None = None
    ssh_public_key_file: Path = Path("~/.ssh/id_rsa.pub")
    # Tailscale
    ts_oauth_client_id: str | None = None
    ts_oauth_client_secret: str | None = None
    tailnet: str | None = None
    ts_suffix: str | None = None
    ts_tag: str = "tag:warden-worker"
    control_host: str | None = None
    # Herdr (hc, the herdr-core CLI behind Herdr.app)
    hc_bin: str = "hc"
    herdr_app_bin: str = "herdr-app"
    known_hosts_file: Path = Path("~/.ssh/known_hosts.warden")
    # Lifetimes
    fleet_max_lifetime_min: int = 120
    ready_timeout_min: int = 15

    def require(self, *names: str) -> None:
        """Raise :class:`ConfigError` naming every listed setting that is unset."""
        missing = [ENV_KEYS[n] for n in names if not getattr(self, n)]
        if missing:
            raise ConfigError(f"missing required setting(s): {', '.join(missing)}")

    def require_do_token(self) -> str:
        if not self.do_token:
            raise MissingCredentialsError("missing DigitalOcean token: set DIGITALOCEAN_TOKEN")
        return self.do_token

    def require_tailscale_oauth(self) -> tuple[str, str]:
        missing = [
            ENV_KEYS[n] for n in ("ts_oauth_client_id", "ts_oauth_client_secret") if not getattr(self, n)
        ]
        if missing:
            raise MissingCredentialsError(f"missing Tailscale OAuth credentials: set {', '.join(missing)}")
        return self.ts_oauth_client_id, self.ts_oauth_client_secret  # type: ignore[return-value]

    @property
    def fleet_temporal_address(self) -> str:
        """Temporal address fleet machines dial over the tailnet (``tailscale serve`` on 7233)."""
        if self.temporal_fleet_address:
            return self.temporal_fleet_address
        self.require("control_host")
        return f"{self.control_host}:7233"

    def tailnet_host(self, machine_name: str) -> str:
        """MagicDNS name of a fleet machine, e.g. ``wf-abcd1234-01.tail1234.ts.net``."""
        self.require("ts_suffix")
        return f"{machine_name}.{self.ts_suffix}"


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build :class:`Settings` from ``env`` (default: ``os.environ``)."""
    env = os.environ if env is None else env
    defaults = Settings()
    values: dict[str, object] = {}
    for f in fields(Settings):
        key = ENV_KEYS[f.name]
        raw = _clean(env.get(key))
        if f.name == "do_token" and raw is None:
            raw = next((v for v in (_clean(env.get(k)) for k in _DO_TOKEN_ALIASES) if v), None)
        default = getattr(defaults, f.name)
        if raw is None:
            values[f.name] = default
        elif isinstance(default, int):
            try:
                values[f.name] = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
        else:
            values[f.name] = raw

    for name in _PATHS:
        values[name] = Path(str(values[name])).expanduser()
    for name in ("fleet_max_lifetime_min", "ready_timeout_min"):
        if int(values[name]) <= 0:  # type: ignore[arg-type]
            raise ConfigError(f"{ENV_KEYS[name]} must be positive")
    return Settings(**values)  # type: ignore[arg-type]
