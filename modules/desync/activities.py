"""scan_chunk: the desync checks on one chunk of URLs, on the worker's machine.

Per URL: the stability gate (the benign request three times), then the smuggle check (a
pipelined benign/attack/benign connection per technique, a gadget-free control when it
fires), then the split check (a fold arm and its matched control per vector, screened
first unless sweeping). Up to CONCURRENCY URLs at once; one pacer holds every host to
rate_ms. Each URL has a time budget; running out of it is reported, not hidden.

Leads and skipped URLs are printed to the slot's tab as they are found. The result
carries desync.jsonl (every observation), desync.txt (leads) and skipped.txt.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import traceback
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from helpers import (
    BUDGET_S,
    CANARY_SUFFIX,
    CHECKS,
    CONCURRENCY,
    REPEATS,
    Baseline,
    Conn,
    Pacer,
    Target,
    analyze_smuggle,
    analyze_split,
    baseline_of,
    benign_request,
    chunk_label,
    control_fired,
    counts,
    exchange,
    fold_vectors,
    heartbeats,
    lead_line,
    now,
    pack,
    printable,
    raw_head,
    render_auto_cl,
    screen_vectors,
    smuggle_techniques,
    split_lead,
    split_request,
    summary,
    target_of,
)
from temporalio import activity


@dataclass
class ScanChunk:
    run_id: str
    index: int
    total: int
    targets: list[str]
    checks: list[str] = field(default_factory=lambda: list(CHECKS))
    sweep: bool = False
    tier: int = 1
    rate_ms: int = 1000
    budget_s: int = BUDGET_S


class Scan:
    """One chunk's state: the pacer, the counters and the three output files."""

    def __init__(self, chunk: ScanChunk) -> None:
        self.chunk = chunk
        self.pacer = Pacer(chunk.rate_ms / 1000)
        self.stats: Counter[str] = Counter()
        self.rows: list[str] = []
        self.leads: list[str] = []
        self.skipped: list[str] = []

    async def connect(self, t: Target, requests: Sequence[bytes]) -> Conn:
        await self.pacer.wait(t.host)
        conn = await exchange(t, requests)
        self.stats["requests"] += conn.sent
        return conn

    def row(self, check: str, url: str, **fields) -> None:
        self.rows.append(json.dumps({"ts": now(), "check": check, "url": url, **fields}))

    def lead(self, check: str, line: str) -> None:
        self.stats[f"leads_{check}"] += 1
        self.leads.append(line)
        print(f"[desync] LEAD {line}", flush=True)

    def skip(self, url: str, reason: str) -> None:
        self.stats["skipped"] += 1
        self.skipped.append(f"{url} {reason}")
        self.row("skipped", url, reason=reason)
        print(f"[desync] skip {url} {reason}", flush=True)


@activity.defn
async def scan_chunk(chunk: ScanChunk) -> dict:
    label = chunk_label(chunk.run_id, chunk.index, chunk.total, len(chunk.targets))
    print(f"\n[desync] >>> {label}: {','.join(chunk.checks)}{' sweep' if chunk.sweep else ''}", flush=True)
    scan = Scan(chunk)
    limit = asyncio.Semaphore(CONCURRENCY)

    async def one(url: str) -> None:
        async with limit:
            try:
                await scan_url(scan, url)
            except Exception as e:  # noqa: BLE001 - one URL's bug must not cost the chunk its other URLs
                traceback.print_exc()
                scan.skip(url, f"error: {type(e).__name__}: {e}")

    beat = asyncio.create_task(heartbeats(lambda: dict(scan.stats)))
    try:
        await asyncio.gather(*(one(u) for u in chunk.targets))
    except asyncio.CancelledError:
        print(f"[desync] <<< {label}: cancelled", flush=True)
        raise
    finally:
        beat.cancel()

    files = pack({"desync.jsonl": scan.rows, "desync.txt": scan.leads, "skipped.txt": scan.skipped})
    s = scan.stats
    leads = {"smuggle": s["leads_smuggle"], "split": s["leads_split"]}
    print(
        f"[desync] <<< {label}: ok, {s['probes']} probes, leads {leads}, {s['skipped']} skipped", flush=True
    )
    return {
        "urls": len(chunk.targets),
        "probed": s["probed"],
        "skipped": s["skipped"],
        "probes": s["probes"],
        "requests": s["requests"],
        "signalled": s["signalled"],
        "withdrawn": s["withdrawn"],
        "leads": leads,
        "warden_files": files,
    }


async def scan_url(scan: Scan, url: str) -> None:
    """Gate, then the chunk's checks, inside the URL's time budget."""
    chunk, t = scan.chunk, target_of(url)
    progress = Counter()
    if "smuggle" in chunk.checks:
        progress["planned"] += len(smuggle_techniques(chunk.sweep))
    if "split" in chunk.checks:
        progress["planned"] += len(fold_vectors(chunk.tier)) if chunk.sweep else 4
    try:
        async with asyncio.timeout(chunk.budget_s):
            benign = benign_request(t, secrets.token_hex(5))
            base = baseline_of([await scan.connect(t, [benign]) for _ in range(REPEATS)])
            scan.row(
                "baseline",
                url,
                stable=base.stable,
                statuses=base.statuses,
                status=base.status,
                digest=base.digest,
                requests=base.requests,
                **({"reason": base.reason} if base.reason else {}),
            )
            if not base.stable:
                scan.skip(url, base.reason)
                return
            scan.stats["probed"] += 1
            if "smuggle" in chunk.checks:
                await smuggle(scan, t, base, benign, progress)
            if "split" in chunk.checks:
                await split(scan, t, progress)
    except TimeoutError:
        scan.skip(url, f"capped: {progress['sent']}/{progress['planned']} probes sent in {chunk.budget_s} s")
        return
    print(f"[desync] done {url}: {progress['sent']} probes", flush=True)


async def smuggle(scan: Scan, t: Target, base: Baseline, benign: bytes, progress: Counter) -> None:
    for tech in smuggle_techniques(scan.chunk.sweep):
        random = secrets.token_hex(5)
        canary = random + CANARY_SUFFIX
        attack = render_auto_cl(tech.text, host=t.authority, endpoint=t.path, random=random)
        conn = await scan.connect(t, [benign, attack, benign])
        progress["sent"] += 1
        scan.stats["probes"] += 1
        signals, void = analyze_smuggle(conn, base, canary)
        fields: dict = {}
        control = None
        if counts(signals):
            scan.stats["signalled"] += 1
            ctl_req = render_auto_cl(tech.control_text, host=t.authority, endpoint=t.path, random=random)
            ctl = await scan.connect(t, [benign, ctl_req, benign])
            fired = control_fired(ctl, base, canary)
            control = "unavailable" if fired is None else "fired" if fired else "quiet"
            fields = {
                "sent": printable(attack),
                "raw_head": raw_head(conn),
                "control_sent": printable(ctl_req),
                "control_answers": [summary(a) for a in ctl.answers],
                "control_raw_head": raw_head(ctl),
                "requests": conn.sent + ctl.sent,
            }
            if fired:
                scan.stats["withdrawn"] += 1
                fields["withdrawn_signals"], signals = signals, []
        lead = counts(signals)
        scan.row(
            "smuggle",
            t.url,
            technique=tech.variant,
            tier=tech.tier,
            provenance=tech.provenance,
            header_line=printable(tech.line),
            canary=canary,
            void=void,
            signals=signals,
            control=control,
            lead=lead,
            answers={k: summary(conn.answer(i)) for i, k in enumerate(("pre", "attack", "post"))},
            trailing_bytes=len(conn.trailing),
            closed_after=conn.closed_after,
            timed_out=conn.timed_out,
            **{"requests": conn.sent, **fields},
        )
        if lead:
            scan.lead("smuggle", lead_line(t.url, "smuggle", tech.variant, printable(tech.line), signals))


async def split(scan: Scan, t: Target, progress: Counter) -> None:
    vectors = fold_vectors(scan.chunk.tier)
    plan = list(vectors) if scan.chunk.sweep else screen_vectors(vectors)
    reacted = False
    for v in plan:
        reacted |= await split_probe(scan, t, v, "sweep" if scan.chunk.sweep else "screen", progress)
    if reacted and not scan.chunk.sweep:
        rest = [v for v in vectors if v not in plan]
        progress["planned"] += len(rest)
        for v in rest:
            await split_probe(scan, t, v, "escalation", progress)


async def split_probe(scan: Scan, t: Target, v, phase: str, progress: Counter) -> bool:
    """One fold arm and its matched control. Returns whether anything moved."""
    canary = f"c{secrets.token_hex(6)}.probe.invalid"
    fold_req, ctl_req = split_request(t, v.encoded, canary), split_request(t, v.control, canary)
    fold = await scan.connect(t, [fold_req])
    ctl = await scan.connect(t, [ctl_req])
    progress["sent"] += 1
    scan.stats["probes"] += 1
    signals, where, void = analyze_split(fold, ctl, v, canary)
    lead = not void and split_lead(v, signals, where)
    fields = {}
    if signals:
        scan.stats["signalled"] += 1
        fields = {"sent": printable(fold_req), "raw_head": raw_head(fold), "control_raw_head": raw_head(ctl)}
    scan.row(
        "split",
        t.url,
        vector=v.id,
        family=v.family,
        cls=v.cls,
        codepoint=v.codepoint,
        encoded=v.encoded,
        control_encoding=v.control,
        tier=v.tier,
        known=v.known,
        note=v.note,
        phase=phase,
        canary=canary,
        void=void,
        signals=signals,
        reflected_in=where,
        lead=lead,
        answers={"fold": summary(fold.answer(0)), "control": summary(ctl.answer(0))},
        requests=fold.sent + ctl.sent,
        **fields,
    )
    if lead:
        scan.lead("split", lead_line(t.url, "split", v.id, v.encoded, signals))
    return bool(signals)
