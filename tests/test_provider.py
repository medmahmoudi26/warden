"""DigitalOcean fleet program (Pulumi mocks) and cloud-init rendering."""

from __future__ import annotations

import asyncio
import itertools

import pulumi
import pytest

from warden.config import load_settings
from warden.fleet.models import FleetSpec
from warden.fleet.provider import (
    UV_SHA256_LINUX_AMD64,
    UV_VERSION,
    DigitalOceanProvider,
    machines_from_outputs,
    make_program,
    render_cloud_init,
)

SETTINGS = load_settings(
    {
        "TAILSCALE_OAUTH_CLIENT_ID": "cid",
        "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
        "WARDEN_TAILNET": "me@example.com",
        "WARDEN_TS_SUFFIX": "tail.ts.net",
    }
)


class Mocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []
        self._ids = itertools.count(1001)

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        outputs = dict(args.inputs)
        if args.typ == "tailscale:index/tailnetKey:TailnetKey":
            outputs["key"] = "tskey-auth-mock"
        if args.typ == "digitalocean:index/droplet:Droplet":
            outputs["ipv4Address"] = "203.0.113.7"
        return [str(next(self._ids)), outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def test_fleet_program_resources() -> None:
    # Pulumi's mock runtime binds to the current event loop; other async tests
    # replace it, so give this test its own loop and fresh mocks.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        mocks = Mocks()
        pulumi.runtime.set_mocks(mocks, preview=False)
        pulumi.runtime.test(lambda: _program_and_checks(mocks))()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _program_and_checks(mocks: Mocks) -> pulumi.Output:
    def by_type(typ: str) -> list[pulumi.runtime.MockResourceArgs]:
        return [r for r in mocks.resources if r.typ == typ]

    res = make_program(
        FleetSpec(fleet_id="abcd1234", instances=2, size="s-2vcpu-4gb"), SETTINGS, "aa:bb:cc"
    )()

    def check(_):
        keys = by_type("tailscale:index/tailnetKey:TailnetKey")
        assert len(keys) == 1
        k = keys[0].inputs
        assert k["ephemeral"] and k["preauthorized"] and k["reusable"]
        assert k["expiry"] == 3600 and k["tags"] == ["tag:warden-worker"]

        droplets = by_type("digitalocean:index/droplet:Droplet")
        assert sorted(d.inputs["name"] for d in droplets) == ["wf-abcd1234-01", "wf-abcd1234-02"]
        for d in droplets:
            assert d.inputs["tags"] == ["warden", "fleet:abcd1234"]
            assert d.inputs["sshKeys"] == ["aa:bb:cc"]
            assert d.inputs["size"] == "s-2vcpu-4gb"  # the fleet's size overrides WARDEN_DO_SIZE
            user_data = d.inputs["userData"]
            assert isinstance(user_data, dict), "user_data (holds the join key) must be a Pulumi secret"
            assert f"--hostname='{d.inputs['name']}'" in user_data["value"]
            assert "tskey-auth-mock" in user_data["value"]

        fws = by_type("digitalocean:index/firewall:Firewall")
        assert len(fws) == 1
        fw = fws[0].inputs
        assert not fw.get("inboundRules")
        assert sorted(r["protocol"] for r in fw["outboundRules"]) == ["icmp", "tcp", "udp"]
        assert len(fw["dropletIds"]) == 2

    created = [res["key"], res["firewall"], *res["droplets"]]
    return pulumi.Output.all(*[r.urn for r in created]).apply(check)


def test_cloud_init_rendering() -> None:
    ci = render_cloud_init("wf-abcd1234-01", "tskey-auth-x", "tag:warden-worker")
    assert ci.startswith("#!/bin/bash")
    assert (
        "tailscale up --auth-key='tskey-auth-x' --hostname='wf-abcd1234-01' --ssh --advertise-tags='tag:warden-worker'"
        in ci
    )
    assert f"releases/download/{UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" in ci
    assert f'{UV_SHA256_LINUX_AMD64}  /tmp/uv.tar.gz" | sha256sum -c -' in ci
    assert ci.index("tailscale up") < ci.index("uv.tar.gz")
    assert "mkdir -p /opt/warden/modules /opt/warden/data /var/lib/warden/slots" in ci
    assert "fallocate -l 2G /swapfile" in ci and "swapon /swapfile" in ci


def test_cloud_init_installs_no_tools_or_hc() -> None:
    ci = render_cloud_init("wf-abcd1234-01", "k", "tag:warden-worker")
    # modules bring their own tools; hc comes from `hc machine add --install`
    assert "subfinder" not in ci and "herdr" not in ci and "hc " not in ci


@pytest.mark.parametrize("bad", [{"authkey": "k'; rm -rf /"}, {"machine": "wf-x\n"}, {"ts_tag": "tag:x'"}])
def test_cloud_init_rejects_unsafe_values(bad) -> None:
    args = {"machine": "wf-abcd1234-01", "authkey": "k", "ts_tag": "tag:warden-worker"}
    args.update(bad)
    with pytest.raises(ValueError):
        render_cloud_init(**args)


def test_machines_have_tailnet_hosts() -> None:
    machines = machines_from_outputs(
        FleetSpec(fleet_id="abcd1234", instances=2),
        SETTINGS,
        {"name_0": "wf-abcd1234-01", "ip_0": "203.0.113.7"},
    )
    assert [(m.name, m.tailnet_host, m.public_ip) for m in machines] == [
        ("wf-abcd1234-01", "wf-abcd1234-01.tail.ts.net", "203.0.113.7"),
        ("wf-abcd1234-02", "wf-abcd1234-02.tail.ts.net", ""),
    ]


def test_destroy_without_stack_is_noop(tmp_path) -> None:
    from warden.config import load_settings as ls

    provider = DigitalOceanProvider(ls({"DIGITALOCEAN_TOKEN": "t", "WARDEN_PULUMI_STATE": str(tmp_path)}))
    assert not provider.has_stack("abcd1234")
    provider.destroy("abcd1234")  # returns without touching Pulumi
    assert not provider.has_stack("abcd1234")
