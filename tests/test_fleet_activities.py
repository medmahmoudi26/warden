"""Fleet activities: fail-fast, readiness gate, module activation, idempotent teardown, sweep."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

import warden.fleet.activities as fa
from tests.fakes import OPERATOR_MACHINE, Dev, FakeClient, FakeHc, FakeHerdr, FakeProvider, FakeTs
from warden.config import MissingCredentialsError, load_settings
from warden.fleet.activities import FleetActivities
from warden.fleet.models import FleetSpec, MachineEntry
from warden.modules import MachineOutcome, Modules

ENV = {
    "DIGITALOCEAN_TOKEN": "do",
    "TAILSCALE_OAUTH_CLIENT_ID": "cid",
    "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
    "WARDEN_TS_SUFFIX": "tail.ts.net",
    "WARDEN_CONTROL_HOST": "mac.tail.ts.net",
}
M1 = "wf-abcd1234-01"
MACHINES = [MachineEntry(name=M1, tailnet_host=f"{M1}.tail.ts.net", public_ip="203.0.113.7")]


def build(env: dict | None = None, **over) -> FleetActivities:
    settings = load_settings(env or ENV)
    parts = {
        "hc": FakeHc(),
        "herdr": FakeHerdr(),
        "tailscale": FakeTs([Dev("n1", M1)]),
        "provider": FakeProvider(),
        "client": FakeClient(),
    }
    parts.update(over)
    modules = parts.pop("modules", None) or Modules(settings, parts["hc"], parts["herdr"])
    return FleetActivities(settings, modules=modules, **parts)


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(fa, "POLL_S", 0.01)


def run(fn, *args):
    return ActivityEnvironment().run(fn, *args)


# --- provision ---------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["DIGITALOCEAN_TOKEN", "TAILSCALE_OAUTH_CLIENT_SECRET"])
async def test_provision_fails_fast_without_credentials(missing) -> None:
    acts = build({k: v for k, v in ENV.items() if k != missing})
    with pytest.raises(MissingCredentialsError, match=missing):
        await run(acts.provision_fleet, FleetSpec(fleet_id="abcd1234"))
    assert acts.provider.ups == []


async def test_provision_returns_machines() -> None:
    acts = build()
    machines = await run(acts.provision_fleet, FleetSpec(fleet_id="abcd1234", instances=2))
    assert [m.name for m in machines] == ["wf-abcd1234-01", "wf-abcd1234-02"]


# --- readiness -----------------------------------------------------------------------


async def test_ready_registers_creates_space_and_probes_temporal() -> None:
    acts = build(hc=FakeHc(add_failures=2))  # SSH not up for the first two tries
    [m] = await run(acts.wait_fleet_ready, "abcd1234", MACHINES, 5)
    assert m.hc_version == "0.1.17"  # differs from the local 0.1.18: still ready
    assert m.public_ip == "203.0.113.7" and m.tailnet_host == f"{M1}.tail.ts.net"
    assert acts.hc.registered[M1]["target"] == f"root@{M1}.tail.ts.net"
    assert M1 in acts.herdr.space_ids
    probes = [c for c in acts.hc.calls if c[0] == "exec" and "</dev/tcp/" in " ".join(c[2])]
    assert probes and "</dev/tcp/mac.tail.ts.net/7233" in probes[0][2][2]
    assert not acts.hc.files and not acts.hc.shipped  # nothing installed by readiness


async def test_ready_temporal_probe_failure_names_machine_and_address() -> None:
    acts = build(hc=FakeHc(temporal_ok=False))
    with pytest.raises(ApplicationError) as exc:
        await run(acts.wait_fleet_ready, "abcd1234", MACHINES, 1)
    msg = str(exc.value)
    assert f"{M1} stuck at temporal" in msg and "mac.tail.ts.net:7233" in msg


async def test_ready_without_herdr_window_still_ready() -> None:
    acts = build(herdr=FakeHerdr(broken=True))
    [m] = await run(acts.wait_fleet_ready, "abcd1234", MACHINES, 5)
    assert m.name == M1


async def test_ready_needs_control_host() -> None:
    acts = build({k: v for k, v in ENV.items() if k != "WARDEN_CONTROL_HOST"})
    with pytest.raises(ApplicationError, match="WARDEN_CONTROL_HOST"):
        await run(acts.wait_fleet_ready, "abcd1234", MACHINES, 1)


async def test_ready_timeout_when_not_on_tailnet() -> None:
    acts = build(tailscale=FakeTs([Dev("n1", M1, online=False)]))
    with pytest.raises(ApplicationError, match="stuck at tailnet"):
        await run(acts.wait_fleet_ready, "abcd1234", MACHINES, 1)


async def test_ready_cloud_init_error_is_fatal() -> None:
    acts = build(hc=FakeHc(boot="status: error\n"))
    with pytest.raises(ApplicationError, match="cloud-init failed"):
        await run(acts.wait_fleet_ready, "abcd1234", MACHINES, 5)


# --- modules at fleet start --------------------------------------------------------------


class StubModules:
    def __init__(self, outcomes: dict[str, list[MachineOutcome] | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple] = []

    def activate(self, fleet_id, machines, module, workers=1):
        self.calls.append((fleet_id, tuple(machines), module, workers))
        out = self.outcomes[module]
        if isinstance(out, Exception):
            raise out
        return out


async def test_activate_modules_returns_errors_instead_of_raising() -> None:
    stub = StubModules(
        {
            "good": [MachineOutcome(M1, started=[1])],
            "half": [MachineOutcome(M1, error="write failed")],
            "missing": LookupError("unknown module 'missing'; available: good"),
        }
    )
    acts = build(modules=stub)
    errors = await run(acts.activate_modules, "abcd1234", [M1], ["good", "half", "missing"])
    assert errors == {"half": f"{M1}: write failed", "missing": "unknown module 'missing'; available: good"}
    assert [c[2:] for c in stub.calls] == [("good", 1), ("half", 1), ("missing", 1)]


# --- teardown --------------------------------------------------------------------------


async def test_terminate_fleet_runs_only_touches_that_fleets_runs() -> None:
    client = FakeClient(
        [
            "run/abcd1234/subdomains-aaaaaa",
            "run/abcd1234/subdomains-aaaaaa/example.com",  # a child: terminated with its parent
            "run/zzzz9999/subdomains-bbbbbb",
            "fleet/abcd1234",
        ]
    )
    acts = build(client=client)
    assert await run(acts.terminate_fleet_runs, "abcd1234") == 1
    assert client.terminated == [("run/abcd1234/subdomains-aaaaaa", "fleet abcd1234 destroyed")]
    assert client.queries == ["ExecutionStatus='Running'"]


async def test_teardown_twice_is_noop_second_time() -> None:
    acts = build()
    await run(acts.wait_fleet_ready, "abcd1234", MACHINES, 5)
    assert await run(acts.unregister_machines, "abcd1234", [M1]) == 1
    assert await run(acts.unregister_machines, "abcd1234", [M1]) == 0
    await run(acts.destroy_fleet, "abcd1234")
    await run(acts.destroy_fleet, "abcd1234")
    assert acts.provider.destroys == ["abcd1234", "abcd1234"]
    assert await run(acts.delete_tailnet_devices, "abcd1234") == 1
    assert await run(acts.delete_tailnet_devices, "abcd1234") == 0
    assert acts.herdr.closed == [M1, M1]
    assert OPERATOR_MACHINE in acts.hc.registered  # the operator's machine is untouched


async def test_prune_known_hosts_uses_warden_file(tmp_path: Path) -> None:
    acts = build({**ENV, "WARDEN_KNOWN_HOSTS_FILE": str(tmp_path / "kh")})
    await run(acts.prune_known_hosts, [f"{M1}.tail.ts.net"])  # missing file: nothing to do


# --- sweep -----------------------------------------------------------------------------


async def test_sweep_destroys_orphans_only(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_do_api(settings, method, path):
        calls.append((method, path))
        if path.startswith("/v2/droplets?tag_name=warden"):
            return {"droplets": [{"name": "wf-orph0001-01"}, {"name": "wf-runn0001-01"}, {"name": "x"}]}
        if path.startswith("/v2/firewalls"):
            return {"firewalls": [{"id": "fw1", "name": "wf-orph0001"}, {"id": "fw2", "name": "wf-runn0001"}]}
        return {}

    monkeypatch.setattr(fa, "do_api", fake_do_api)
    hc = FakeHc()
    hc.registered.update(
        {
            "wf-orph0001-01": {"name": "wf-orph0001-01", "state": "connected"},
            "wf-runn0001-01": {"name": "wf-runn0001-01", "state": "connected"},
        }
    )
    provider = FakeProvider()
    provider.stacks.add("orph0001")
    herdr = FakeHerdr()
    acts = build(
        hc=hc,
        herdr=herdr,
        provider=provider,
        tailscale=FakeTs([Dev("o1", "wf-orph0001-01"), Dev("r1", "wf-runn0001-01")]),
        client=FakeClient(["fleet/runn0001"]),
    )
    out = await run(acts.sweep_orphans)
    assert out == {"running": ["runn0001"], "orphans": ["orph0001"]}
    assert provider.destroys == ["orph0001"]
    assert ("DELETE", "/v2/droplets?tag_name=fleet:orph0001") in calls
    assert ("DELETE", "/v2/firewalls/fw1") in calls and ("DELETE", "/v2/firewalls/fw2") not in calls
    assert acts.tailscale.deleted == ["o1"]
    assert hc.removed_machines == ["wf-orph0001-01"] and herdr.closed == ["wf-orph0001-01"]
    assert OPERATOR_MACHINE in hc.registered and "wf-runn0001-01" in hc.registered
