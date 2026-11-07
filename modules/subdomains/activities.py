"""The steps of reconftw's subdomain pipeline, one activity each, run on the worker's machine.

* ``passive``        subfinder (all sources, config/provider-config.yaml) and ip.thc.org
* ``resolve``        puredns: public resolvers, trusted-resolver validation, wildcard filtering
* ``bruteforce``     puredns bruteforce with reconftw's wordlist (n0kovo's huge list when deep)
* ``permute``        one gotator round, its candidates resolved on the same machine
* ``regex_permute``  regulator's patterns, their candidates resolved on the same machine

DomainEnum (workflows.py) chains them. Names travel between steps as activity
inputs and results. Only gotator's candidates, which can reach 2 GB, are
generated and resolved inside a single activity.

Every tool's output streams into the slot's terminal (its Herdr tab). Results
also carry files for Warden to assemble: what ``passive`` finds goes into
passive.txt, and what any resolving step finds goes into subdomains.txt.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from helpers import (
    begin,
    data_dir,
    in_scope,
    provider_config,
    puredns_resolve,
    read_lines,
    regulator_command,
    resolvers,
    run_step,
    run_tool,
    say,
    thc,
    tool,
    warden_file,
    write_lines,
)
from temporalio import activity

# reconftw.cfg
GOTATOR_FLAGS = ["-depth", "1", "-numbers", "3", "-mindup", "-adv", "-md"]
PERMUTATIONS_LIMIT = 2 * 1024**3  # bytes of gotator candidates per round


@dataclass
class Names:
    """What a step found (lowercased names under the domain), plus files for Warden."""

    names: list[str]
    note: str = ""
    warden_files: dict[str, str] = field(default_factory=dict)


@dataclass
class PassiveInput:
    domain: str
    max_minutes: int = 180  # subfinder's -max-time


@dataclass
class ResolveInput:
    domain: str
    step: str  # names the step in the tab
    names: list[str]


@dataclass
class BruteforceInput:
    domain: str
    deep: bool = False


@dataclass
class PermuteInput:
    domain: str
    step: str
    names: list[str]
    full_wordlist: bool  # reconftw's full permutation list, else its short one


@dataclass
class RegexPermuteInput:
    domain: str
    names: list[str]


@activity.defn
async def passive(req: PassiveInput) -> Names:
    domain = req.domain
    begin(domain, "passive", f"subfinder (at most {req.max_minutes} minutes) and ip.thc.org")
    with tempfile.TemporaryDirectory(prefix="subdomains-") as tmp:
        out = Path(tmp) / "subfinder.txt"
        argv = [tool("subfinder"), "-d", domain, "-all", "-silent", "-max-time", str(req.max_minutes)]
        config = provider_config()
        if config.is_file():
            argv += ["-pc", str(config)]
        else:
            say(domain, "passive", "no provider config: keyless sources only")
        await run_step(domain, "passive", "subfinder", [*argv, "-o", str(out)])
        names = in_scope(domain, read_lines(out)) | in_scope(domain, await thc(domain))
    say(domain, "passive", f"done: {len(names)} names")
    return Names(sorted(names), warden_files=warden_file("passive.txt", names))


@activity.defn
async def resolve(req: ResolveInput) -> Names:
    names = in_scope(req.domain, req.names)
    begin(req.domain, req.step, f"resolving {len(names)} names (puredns)")
    if not names:
        return Names([])
    with tempfile.TemporaryDirectory(prefix="subdomains-") as tmp:
        candidates = write_lines(Path(tmp) / "candidates.txt", names)
        found = await puredns_resolve(req.domain, req.step, candidates, Path(tmp))
    say(req.domain, req.step, f"done: {len(found)} resolve")
    return Names(sorted(found), warden_files=warden_file("subdomains.txt", found))


@activity.defn
async def bruteforce(req: BruteforceInput) -> Names:
    wordlist = data_dir() / ("n0kovo_subdomains_huge.txt" if req.deep else "subdomains.txt")
    begin(req.domain, "bruteforce", f"{wordlist.name} (puredns)")
    with tempfile.TemporaryDirectory(prefix="subdomains-") as tmp:
        out = Path(tmp) / "bruteforce.txt"
        argv = [tool("puredns"), "bruteforce", str(wordlist), req.domain, "-w", str(out), *resolvers()]
        await run_step(req.domain, "bruteforce", "puredns bruteforce", argv)
        names = in_scope(req.domain, read_lines(out))
    say(req.domain, "bruteforce", f"done: {len(names)} names")
    return Names(sorted(names))  # resolved again by the workflow, as reconftw does


@activity.defn
async def permute(req: PermuteInput) -> Names:
    wordlist = data_dir() / ("permutations_list.txt" if req.full_wordlist else "permutations_list_short.txt")
    begin(req.domain, req.step, f"{len(req.names)} names x {wordlist.name} (gotator)")
    note = ""
    with tempfile.TemporaryDirectory(prefix="subdomains-") as tmp:
        source = write_lines(Path(tmp) / "names.txt", req.names)
        candidates = Path(tmp) / "candidates.txt"
        written = 0
        with candidates.open("w") as out:

            def keep(line: str) -> bool:  # like reconftw's `| head -c PERMUTATIONS_LIMIT`
                nonlocal written
                written += len(line) + 1
                if written > PERMUTATIONS_LIMIT:
                    return False
                out.write(f"{line}\n")
                return True

            argv = [tool("gotator"), "-sub", str(source), "-perm", str(wordlist), *GOTATOR_FLAGS, "-silent"]
            await run_step(req.domain, req.step, "gotator", argv, on_line=keep)
        if written > PERMUTATIONS_LIMIT:
            note = f"{req.step}: candidates capped at {PERMUTATIONS_LIMIT} bytes"
            say(req.domain, req.step, note)
        found = await puredns_resolve(req.domain, req.step, candidates, Path(tmp)) if written else set()
    say(req.domain, req.step, f"done: {len(found)} resolve")
    return Names(sorted(found), note, warden_file("subdomains.txt", found))


@activity.defn
async def regex_permute(req: RegexPermuteInput) -> Names:
    begin(req.domain, "regex", f"learning from {len(req.names)} names (regulator)")
    with tempfile.TemporaryDirectory(prefix="subdomains-") as tmp:
        source = write_lines(Path(tmp) / "names.txt", req.names)
        candidates = Path(tmp) / "candidates.txt"
        argv, cwd = regulator_command()
        rc = await run_tool([*argv, "-t", req.domain, "-f", str(source), "-o", str(candidates)], cwd=cwd)
        if rc != 0 or not candidates.exists():  # reconftw carries on without regulator's candidates
            note = f"regulator exited {rc}; regex permutations skipped"
            say(req.domain, "regex", note)
            return Names([], note)
        found = await puredns_resolve(req.domain, "regex", candidates, Path(tmp))
    say(req.domain, "regex", f"done: {len(found)} resolve")
    return Names(sorted(found), warden_files=warden_file("subdomains.txt", found))
