"""modules/subdomains: SubdomainEnum, a DomainEnum child per domain, and one activity per reconftw step,
on a dev server with fake tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError

from tests.modules.conftest import alive, all_gone, calls, load, wait_for_pids, worker
from warden.runs import collect_files

TQ = "test.subdomains"
RID = "run/test0001/subdomains-aaaaaa"


@pytest.fixture
async def subdomains(dev_server, tools, monkeypatch):
    m = load("subdomains")
    a = m.activities
    workflows = [m.workflows.SubdomainEnum, m.workflows.DomainEnum]
    activities = [a.passive, a.resolve, a.bruteforce, a.permute, a.regex_permute]
    async with worker(dev_server.client, m, workflows, activities, TQ, monkeypatch):
        yield m


async def enumerate(client, subdomains, domains, rid=RID, **opts) -> dict:
    return await client.execute_workflow(
        subdomains.workflows.SubdomainEnum.run,
        subdomains.workflows.SubdomainEnumInput(domains=domains, **opts),
        id=rid,
        task_queue=TQ,
    )


async def test_every_step_runs_and_files_come_from_the_children(
    subdomains, dev_server, tools, tmp_path
) -> None:
    res = await enumerate(dev_server.client, subdomains, ["example.com"])
    c = res["domains"]["example.com"]
    # passive: sub, www, dead-old (unrelated.example.org is out of scope); resolved: sub, www and the domain
    assert (c["passive"], c["resolved"]) == (3, 3)
    assert c["bruteforce"] == 2  # vpn, mail
    # gotator round 1 on the 5 names gives dev-<each> (dev-example.com is out of scope, dead-* never
    # resolve): 4; round 2 adds dev-dev-<each>: 4; regulator adds api-prod-2
    assert c["permutations"] == 8 and c["regex"] == 1 and c["total"] == 14 == res["count"]
    assert res["domains_failed"] == [] and res["totals"]["total"] == 14

    files = await collect_files(dev_server.client, RID, tmp_path)  # follows the DomainEnum child
    found = Path(files["subdomains.txt"]["path"]).read_text().splitlines()
    assert found == sorted(found) and len(found) == 14 and "dead-old.example.com" not in found
    assert {"example.com", "vpn.example.com", "dev-dev-sub.example.com", "api-prod-2.example.com"} <= set(
        found
    )
    passive = Path(files["passive.txt"]["path"]).read_text().splitlines()
    assert passive == ["dead-old.example.com", "sub.example.com", "www.example.com"]

    (subfinder,) = calls(tools, "subfinder")
    assert subfinder[:3] == ["-d", "example.com", "-all"] and "-pc" not in subfinder
    assert subfinder[subfinder.index("-max-time") + 1] == "180"  # reconftw's default
    brute = calls(tools, "puredns")[1]
    assert brute[:3] == ["bruteforce", str(tools.parent / "data" / "subdomains.txt"), "example.com"]
    assert brute[brute.index("-l") + 1] == "5000" and "--resolvers-trusted" in brute
    gotator = calls(tools, "gotator")
    assert len(gotator) == 2 and gotator[0][gotator[0].index("-perm") + 1].endswith("permutations_list.txt")
    assert "-mindup" in gotator[0] and calls(tools, "regulator")[0][:2] == ["-t", "example.com"]

    history = dev_server.client.get_workflow_handle(f"{RID}/example.com").fetch_history_events()
    steps = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        async for e in history
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert steps == ["passive", "resolve", "bruteforce", "resolve", "permute", "permute", "regex_permute"]


async def test_a_failed_step_is_retried_alone(subdomains, dev_server, tools) -> None:
    res = await enumerate(dev_server.client, subdomains, ["bruteagain.example.com"])
    assert res["domains"]["bruteagain.example.com"]["bruteforce"] == 2
    assert [a[0] for a in calls(tools, "puredns")].count("bruteforce") == 2  # failed once, retried
    assert len(calls(tools, "subfinder")) == 1  # the passive step was not redone
    assert [a[0] for a in calls(tools, "puredns")].count("resolve") == 5  # passive, brute, 2 rounds, regex


async def test_provider_config_and_deep_wordlists(
    subdomains, dev_server, tools, tmp_path, monkeypatch
) -> None:
    config = tmp_path / "provider-config.yaml"
    config.write_text("shodan: [k]\n")
    monkeypatch.setenv("SUBFINDER_PROVIDER_CONFIG", str(config))
    await enumerate(dev_server.client, subdomains, ["example.com"], deep=True, passive_minutes=15)
    (subfinder,) = calls(tools, "subfinder")
    assert subfinder[subfinder.index("-pc") + 1] == str(config)
    assert subfinder[subfinder.index("-max-time") + 1] == "15"
    assert calls(tools, "puredns")[1][1].endswith("n0kovo_subdomains_huge.txt")


async def test_passive_only(subdomains, dev_server, tools) -> None:
    domains = [f"d{i}.example.com" for i in range(4)]
    res = await enumerate(dev_server.client, subdomains, domains, bruteforce=False, permutations=False)
    assert res["count"] == 4 * 3 and set(res["domains"]) == set(domains)  # sub, www and the domain each
    assert calls(tools, "gotator") == [] and [a[0] for a in calls(tools, "puredns")] == ["resolve"] * 4


async def test_reconftw_permutation_limits(subdomains, dev_server, tools, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_LINES", "600")  # 602 resolved names: over DEEP_LIMIT, under DEEP_LIMIT2
    res = await enumerate(dev_server.client, subdomains, ["example.com"], bruteforce=False)
    first = calls(tools, "gotator")[0]
    assert first[first.index("-perm") + 1].endswith("permutations_list_short.txt")  # more than 100 names
    assert "regulator skipped" not in str(res)  # 600-odd names are under regulator's 2,000

    monkeypatch.setenv("FAKE_LINES", "2000")  # over both limits: gotator and regulator are skipped
    res = await enumerate(
        dev_server.client, subdomains, ["big.example.com"], rid=f"{RID[:-1]}b", bruteforce=False
    )
    notes = res["domains"]["big.example.com"]["notes"]
    assert notes[0].startswith("gotator skipped") and notes[1].startswith("regulator skipped")


async def test_gotator_candidates_are_capped(subdomains, dev_server, tools, monkeypatch) -> None:
    monkeypatch.setattr(subdomains.activities, "PERMUTATIONS_LIMIT", 200)
    monkeypatch.setenv("FAKE_GOTATOR_REPEAT", "100000")  # far more than 200 bytes
    res = await enumerate(dev_server.client, subdomains, ["example.com"])
    assert any(
        n.startswith("permutations-1: candidates capped") for n in res["domains"]["example.com"]["notes"]
    )


async def test_failed_domains_are_reported(subdomains, dev_server, tools) -> None:
    res = await enumerate(
        dev_server.client, subdomains, ["a.example.com", "fail.example.com"], permutations=False
    )
    assert set(res["domains"]) == {"a.example.com"}
    assert res["domains_failed"] == [
        {"domain": "fail.example.com", "error": "subfinder exited 2 on fail.example.com"}
    ]
    assert len([a for a in calls(tools, "subfinder") if a[1] == "fail.example.com"]) == 3  # 3 attempts


async def test_all_domains_failing_fails_the_run(subdomains, dev_server, tools) -> None:
    with pytest.raises(WorkflowFailureError) as exc:
        await enumerate(dev_server.client, subdomains, ["fail1.example.com", "fail2.example.com"])
    assert "all 2 domains failed" in str(exc.value.cause)


async def test_large_passive_results_fit_through_the_codec(dev_server, tools, tmp_path, monkeypatch) -> None:
    """example.com's 62,937 passive names failed the 2 MB limit uncompressed; with the codec they fit."""
    m = load("subdomains")
    target = dev_server.client.service_client.config.target_host
    client = await Client.connect(target, data_converter=m.helpers.data_converter())
    a = m.activities
    workflows = [m.workflows.SubdomainEnum, m.workflows.DomainEnum]
    activities = [a.passive, a.resolve, a.bruteforce, a.permute, a.regex_permute]
    monkeypatch.setenv("FAKE_LINES", "70000")
    async with worker(client, m, workflows, activities, TQ, monkeypatch):
        res = await enumerate(client, m, ["big.example.com"], bruteforce=False, permutations=False)
    assert res["domains"]["big.example.com"]["passive"] == 70003  # the padding plus sub, www, dead-old
    files = await collect_files(client, RID, tmp_path)
    assert files["passive.txt"]["lines"] == 70003
    assert files["subdomains.txt"]["lines"] == 70003  # dead-old does not resolve; the domain does


async def test_too_many_names_for_a_payload_is_not_retried(
    subdomains, dev_server, tools, monkeypatch
) -> None:
    monkeypatch.setattr(subdomains.helpers, "MAX_RESULT_BYTES", 2000)
    monkeypatch.setenv("FAKE_LINES", "5000")
    with pytest.raises(WorkflowFailureError) as exc:
        await enumerate(
            dev_server.client, subdomains, ["big.example.com"], bruteforce=False, permutations=False
        )
    assert "too many for one Temporal payload" in str(exc.value.cause) and len(calls(tools, "subfinder")) == 1


async def test_cancellation_kills_the_tools(subdomains, dev_server, tools) -> None:
    handle = await dev_server.client.start_workflow(
        subdomains.workflows.SubdomainEnum.run,
        subdomains.workflows.SubdomainEnumInput(domains=["slow.example.com"]),
        id=RID,
        task_queue=TQ,
    )
    pids = await wait_for_pids(tools)
    assert all(alive(p) for p in pids)
    await handle.cancel()
    with pytest.raises(WorkflowFailureError):
        await handle.result()
    assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED
    assert await all_gone(pids), "subfinder and its child must be gone"
