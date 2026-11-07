"""reconftw-style subdomain enumeration, orchestrated step by step.

SubdomainEnum starts one DomainEnum child workflow per root domain. DomainEnum
runs reconftw's steps for its domain as activities:

    passive -> resolve -> bruteforce -> resolve -> permute -> permute -> regex_permute

Each step has its own timeout and retries. A failed step is retried alone, on
any worker, and finished steps are never redone: their results are in the
child's history.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from activities import (
        BruteforceInput,
        PassiveInput,
        PermuteInput,
        RegexPermuteInput,
        ResolveInput,
        bruteforce,
        passive,
        permute,
        regex_permute,
        resolve,
    )
    from helpers import error_message, is_cancellation

# Step timeouts: subfinder's own limit (passive_minutes) plus a margin, then reconftw's
# DNS_RESOLVE_TIMEOUT and DNS_BRUTE_TIMEOUT.
PASSIVE_MARGIN = timedelta(minutes=15)
RESOLVE_TIMEOUT = timedelta(hours=4)
BRUTEFORCE_TIMEOUT = timedelta(hours=6)
PERMUTE_TIMEOUT = timedelta(hours=6)
REGEX_TIMEOUT = timedelta(hours=4)
HEARTBEAT_TIMEOUT = timedelta(minutes=2)  # a lost worker is noticed within 2 minutes; blips are survived
STEP_RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=5))

# reconftw.cfg
DEEP_LIMIT = 500  # gotator permutes the resolved names up to this many...
DEEP_LIMIT2 = 1500  # ...else the unresolved passive names up to this many, else nothing
PERMUTATIONS_SHORT_THRESHOLD = 100  # above this many names, the short permutation list
REGULATOR_MAX_NAMES = 2000  # regulator's distance table grows with the square of its input

MAX_CONCURRENT_DOMAINS = 100  # DomainEnum children running at once
DETAIL_MAX_DOMAINS = 1000  # per-domain counts in the result up to this many domains
STEPS = ("passive", "resolved", "bruteforce", "permutations", "regex", "total")


@dataclass
class SubdomainEnumInput:
    domains: list[str]
    bruteforce: bool = True
    permutations: bool = True
    deep: bool = False
    passive_minutes: int = 180  # subfinder's -max-time (reconftw's SUBFINDER_ENUM_TIMEOUT)


@dataclass
class DomainEnumInput:
    domain: str
    bruteforce: bool = True
    permutations: bool = True
    deep: bool = False
    passive_minutes: int = 180


@workflow.defn
class SubdomainEnum:
    @workflow.run
    async def run(self, req: SubdomainEnumInput) -> dict:
        domains = list(dict.fromkeys(d.strip().lower() for d in req.domains if d.strip()))
        run_id = workflow.info().workflow_id
        slots = asyncio.Semaphore(MAX_CONCURRENT_DOMAINS)

        async def enumerate_domain(domain: str) -> dict:
            async with slots:
                return await workflow.execute_child_workflow(
                    DomainEnum.run,
                    DomainEnumInput(domain, req.bruteforce, req.permutations, req.deep, req.passive_minutes),
                    id=f"{run_id}/{domain}",
                )

        results = await asyncio.gather(*(enumerate_domain(d) for d in domains), return_exceptions=True)

        per_domain, failed = {}, []
        for domain, result in zip(domains, results, strict=True):
            if isinstance(result, BaseException):
                if is_cancellation(result):
                    raise result
                failed.append({"domain": domain, "error": error_message(result)})
            else:
                per_domain[domain] = result
        if domains and not per_domain:
            raise ApplicationError(f"all {len(domains)} domains failed; last error: {failed[-1]['error']}")

        out = {
            "count": sum(c["total"] for c in per_domain.values()),
            "totals": {step: sum(c[step] for c in per_domain.values()) for step in STEPS},
            "domains_total": len(domains),
            "domains_failed": failed,
            "options": {
                "bruteforce": req.bruteforce,
                "permutations": req.permutations,
                "deep": req.deep,
                "passive_minutes": req.passive_minutes,
            },
        }
        if len(per_domain) <= DETAIL_MAX_DOMAINS:
            out["domains"] = per_domain
        return out


@workflow.defn
class DomainEnum:
    """reconftw's steps for one root domain."""

    def __init__(self) -> None:
        self.found: set[str] = set()
        self.counts = dict.fromkeys(STEPS, 0)
        self.notes: list[str] = []

    @workflow.run
    async def run(self, req: DomainEnumInput) -> dict:
        domain = req.domain

        passive_timeout = timedelta(minutes=req.passive_minutes) + PASSIVE_MARGIN
        passive_names = await self.step(passive, PassiveInput(domain, req.passive_minutes), passive_timeout)
        self.found = set(await self.resolve(domain, "passive", [*passive_names, domain]))
        self.counts["passive"], self.counts["resolved"] = len(passive_names), len(self.found)

        if req.bruteforce:
            candidates = await self.step(bruteforce, BruteforceInput(domain, req.deep), BRUTEFORCE_TIMEOUT)
            self.add("bruteforce", await self.resolve(domain, "bruteforce", candidates))

        if req.permutations:
            self.add("permutations", await self.gotator(domain, passive_names, req.deep))
            self.add("regex", await self.regulator(domain))

        self.counts["total"] = len(self.found)
        return {**self.counts, "notes": self.notes}

    async def gotator(self, domain: str, passive_names: list[str], deep: bool) -> list[str]:
        """Two rounds; the second permutes what the first resolved (reconftw's sub_permut)."""
        if deep or len(self.found) <= DEEP_LIMIT:
            source = sorted(self.found)
        elif len(passive_names) <= DEEP_LIMIT2:
            source = passive_names
        else:
            self.notes.append(
                f"gotator skipped: {len(self.found)} resolved and {len(passive_names)} passive names"
            )
            return []
        first = await self.permutation_round(domain, "permutations-1", source, deep)
        second = await self.permutation_round(domain, "permutations-2", first, deep) if first else []
        return first + second

    async def permutation_round(self, domain: str, step: str, names: list[str], deep: bool) -> list[str]:
        full = deep or len(names) <= PERMUTATIONS_SHORT_THRESHOLD
        return await self.step(permute, PermuteInput(domain, step, names, full), PERMUTE_TIMEOUT)

    async def regulator(self, domain: str) -> list[str]:
        names = sorted(self.found - {domain})
        if len(names) < 2:
            return []
        if len(names) > REGULATOR_MAX_NAMES:
            self.notes.append(f"regulator skipped: {len(names)} names (limit {REGULATOR_MAX_NAMES})")
            return []
        return await self.step(regex_permute, RegexPermuteInput(domain, names), REGEX_TIMEOUT)

    async def resolve(self, domain: str, step: str, names: list[str]) -> list[str]:
        return await self.step(resolve, ResolveInput(domain, step, names), RESOLVE_TIMEOUT)

    async def step(self, activity, arg, timeout: timedelta) -> list[str]:
        """Run one step's activity; its names, with any note it made recorded."""
        result = await workflow.execute_activity(
            activity,
            arg,
            start_to_close_timeout=timeout,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
            retry_policy=STEP_RETRY,
        )
        if result.note:
            self.notes.append(result.note)
        return result.names

    def add(self, step: str, names: list[str]) -> None:
        new = set(names) - self.found
        self.counts[step] = len(new)
        self.found |= new
