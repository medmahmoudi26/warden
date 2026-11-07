"""DigitalOcean fleets through the Pulumi Automation API (local file backend).

One stack per fleet, ``warden-fleet-<fleet_id>``, holding:

* ``tailscale.TailnetKey``: ephemeral, preauthorized, reusable (N machines),
  1 h expiry, tagged ``tag:warden-worker``;
* N droplets ``wf-<fleet_id>-NN`` tagged ``warden`` + ``fleet:<fleet_id>``,
  whose cloud-init joins the tailnet and installs ``uv`` (modules bring
  their own tools);
* a firewall with **no inbound rules** (Tailscale needs only outbound).

``hc`` is not installed here: ``hc machine add --install`` copies it during the
readiness gate, so its version always matches the control plane.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from warden.config import MissingCredentialsError, Settings
from warden.fleet.models import FleetSpec, MachineEntry, machine_name, stack_name

logger = logging.getLogger("warden.fleet.provider")

UV_VERSION = "0.9.3"
UV_SHA256_LINUX_AMD64 = "4d6f84490da4b21bb6075ffc1c6b22e0cf37bc98d7cca8aff9fbb759093cdc23"
MODULES_ROOT = "/opt/warden/modules"
DATA_ROOT = "/opt/warden/data"  # module tools and wordlists; survives module re-shipping
SWAP_SIZE = "2G"
SLOTS_DIR = "/var/lib/warden/slots"
ALL_ADDRESSES = ["0.0.0.0/0", "::/0"]


def render_cloud_init(machine: str, authkey: str, ts_tag: str) -> str:
    """Bash user-data for one fleet machine.

    Tailscale joins first, so a later failure can be debugged over Tailscale SSH
    (``cloud-init status --long``). The readiness gate waits for ``done``.
    """
    for value, what in ((machine, "machine"), (authkey, "authkey"), (ts_tag, "tag")):
        if "'" in value or "\n" in value:
            raise ValueError(f"invalid {what} for cloud-init")
    uv = UV_VERSION
    return f"""#!/bin/bash
# Warden fleet machine {machine} (generated)
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl ca-certificates unzip

# 1. Tailscale (early, for debuggability)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --auth-key='{authkey}' --hostname='{machine}' --ssh --advertise-tags='{ts_tag}'

# 2. uv {uv} (sha256-verified); modules run their Python with it
curl -fsSL --retry 5 -o /tmp/uv.tar.gz \\
  "https://github.com/astral-sh/uv/releases/download/{uv}/uv-x86_64-unknown-linux-gnu.tar.gz"
echo "{UV_SHA256_LINUX_AMD64}  /tmp/uv.tar.gz" | sha256sum -c -
tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1
rm -f /tmp/uv.tar.gz

# 3. Swap, so small droplets survive memory peaks (nuclei) instead of OOM kills
if ! swapon --show | grep -q /swapfile; then
  fallocate -l {SWAP_SIZE} /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 4. Module folders, module data and slot exit files
mkdir -p {MODULES_ROOT} {DATA_ROOT} {SLOTS_DIR}
"""


def ssh_fingerprint(settings: Settings) -> str:
    """MD5 fingerprint DigitalOcean accepts in ``Droplet.ssh_keys`` (break-glass console access)."""
    if settings.do_ssh_fingerprint:
        return settings.do_ssh_fingerprint
    path = Path(settings.ssh_public_key_file).expanduser()
    if not path.is_file():
        raise RuntimeError(f"SSH public key not found at {path} (set WARDEN_SSH_PUBLIC_KEY_FILE)")
    parts = path.read_text().strip().split()
    if len(parts) < 2:
        raise RuntimeError(f"invalid SSH public key in {path}")
    digest = hashlib.md5(base64.b64decode(parts[1])).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, 32, 2))


def do_api(settings: Settings, method: str, path: str) -> dict:
    """Minimal DigitalOcean REST call (used by the orphan sweep). 404 is an empty result."""
    req = urllib.request.Request(f"https://api.digitalocean.com{path}", method=method)
    req.add_header("Authorization", f"Bearer {settings.require_do_token()}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def make_program(spec: FleetSpec, settings: Settings, fingerprint: str):
    """Pulumi inline program for one fleet."""
    region = spec.region or settings.do_region
    image = spec.image or settings.do_image
    size = spec.size or settings.do_size

    def program() -> dict:
        import pulumi
        import pulumi_digitalocean as do
        import pulumi_tailscale as ts

        client_id, client_secret = settings.require_tailscale_oauth()
        ts_provider = ts.Provider(
            "tailscale",
            oauth_client_id=client_id,
            oauth_client_secret=pulumi.Output.secret(client_secret),
            tailnet=settings.tailnet or "-",
        )
        key = ts.TailnetKey(
            "join-key",
            reusable=True,
            ephemeral=True,
            preauthorized=True,
            expiry=3600,
            tags=[settings.ts_tag],
            description=f"warden-{spec.fleet_id}",
            recreate_if_invalid="always",
            opts=pulumi.ResourceOptions(provider=ts_provider),
        )

        droplets = []
        for i in range(spec.instances):
            name = machine_name(spec.fleet_id, i)
            user_data = key.key.apply(lambda k, name=name: render_cloud_init(name, k, settings.ts_tag))
            droplet = do.Droplet(
                name,
                name=name,
                region=region,
                size=size,
                image=image,
                ssh_keys=[fingerprint],
                user_data=pulumi.Output.secret(user_data),
                tags=["warden", f"fleet:{spec.fleet_id}"],
            )
            droplets.append(droplet)
            pulumi.export(f"name_{i}", droplet.name)
            pulumi.export(f"ip_{i}", droplet.ipv4_address)

        firewall = do.Firewall(
            f"wf-{spec.fleet_id}",
            name=f"wf-{spec.fleet_id}",
            droplet_ids=[d.id.apply(int) for d in droplets],
            outbound_rules=[
                do.FirewallOutboundRuleArgs(
                    protocol="tcp", port_range="all", destination_addresses=ALL_ADDRESSES
                ),
                do.FirewallOutboundRuleArgs(
                    protocol="udp", port_range="all", destination_addresses=ALL_ADDRESSES
                ),
                do.FirewallOutboundRuleArgs(protocol="icmp", destination_addresses=ALL_ADDRESSES),
            ],
        )
        pulumi.export("fleet_id", spec.fleet_id)
        # Returned for tests; the Automation API ignores it.
        return {"key": key, "droplets": droplets, "firewall": firewall}

    return program


def machines_from_outputs(spec: FleetSpec, settings: Settings, outputs: dict[str, Any]) -> list[MachineEntry]:
    def value(name: str) -> Any:
        out = outputs.get(name)
        return getattr(out, "value", out)

    machines = []
    for i in range(spec.instances):
        name = str(value(f"name_{i}") or machine_name(spec.fleet_id, i))
        machines.append(
            MachineEntry(
                name=name, tailnet_host=settings.tailnet_host(name), public_ip=str(value(f"ip_{i}") or "")
            )
        )
    return machines


class DigitalOceanProvider:
    """Provision and destroy fleet stacks. Both operations are idempotent."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def work_dir(self, fleet_id: str) -> Path:
        path = Path(self.settings.pulumi_state_dir).expanduser().resolve() / fleet_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def has_stack(self, fleet_id: str) -> bool:
        return (Path(self.settings.pulumi_state_dir).expanduser() / fleet_id / "Pulumi.yaml").exists()

    def _stack(self, fleet_id: str, program):
        from pulumi import automation as auto

        os.environ.setdefault("PULUMI_CONFIG_PASSPHRASE", "warden-local")
        stack = auto.create_or_select_stack(
            stack_name=stack_name(fleet_id),
            project_name=f"warden-{fleet_id}",
            program=program,
            opts=auto.LocalWorkspaceOptions(
                project_settings=auto.ProjectSettings(name=f"warden-{fleet_id}", runtime="python"),
                work_dir=str(self.work_dir(fleet_id)),
            ),
        )
        stack.set_config(
            "digitalocean:token", auto.ConfigValue(value=self.settings.require_do_token(), secret=True)
        )
        return stack

    def up(self, spec: FleetSpec) -> list[MachineEntry]:
        s = self.settings
        s.require_do_token()
        s.require_tailscale_oauth()
        s.require("ts_suffix")
        stack = self._stack(spec.fleet_id, make_program(spec, s, ssh_fingerprint(s)))
        for plugin, package in (("digitalocean", "pulumi_digitalocean"), ("tailscale", "pulumi_tailscale")):
            stack.workspace.install_plugin(plugin, importlib.metadata.version(package))
        logger.info("pulumi up stack=%s instances=%s", stack_name(spec.fleet_id), spec.instances)
        result = stack.up(on_output=lambda line: logger.info("%s", line))
        return machines_from_outputs(spec, s, result.outputs or {})

    def destroy(self, fleet_id: str) -> None:
        """Destroy the stack. A missing stack (or missing credentials) is a no-op."""
        try:
            self.settings.require_do_token()
        except MissingCredentialsError:
            logger.warning("destroy %s: no DigitalOcean token; skipping", fleet_id)
            return
        if not self.has_stack(fleet_id):
            logger.info("destroy %s: no stack", fleet_id)
            return
        stack = self._stack(fleet_id, lambda: None)
        logger.info("pulumi destroy stack=%s", stack_name(fleet_id))
        stack.destroy(on_output=lambda line: logger.info("%s", line))
        try:
            stack.workspace.remove_stack(stack_name(fleet_id))
        except Exception:  # noqa: BLE001 — the resources are gone; the stack record is cosmetic
            pass
