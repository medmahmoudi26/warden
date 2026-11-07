"""modules/desync: the wire layer, the technique family, the fold vectors and the oracles, then
scan_chunk and DesyncScan against a raw-socket lab whose ground truth is known."""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import socket
import time
from pathlib import Path

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.testing import ActivityEnvironment

from tests.modules.conftest import load, worker
from tests.modules.desync_lab import Lab
from warden.runs import collect_files

TQ = "test.desync"
SEED_LINES = [
    "\tContent-Length: ${cl}",
    "Content-Length\t:\t${cl}",
    "Content-Length \r\n : ${cl}",
    "Content-Length: +${cl}",
    "content-length: +${cl}",
]


@pytest.fixture
def desync(monkeypatch):
    m = load("desync")
    monkeypatch.setattr(m.helpers, "GRACE_S", 0.05)
    monkeypatch.setattr(m.helpers, "READ_TIMEOUT_S", 2.0)
    monkeypatch.setattr(m.helpers, "HEARTBEAT_EVERY_S", 0.2)
    return m


async def talk(lab: Lab, data: bytes, idle: float = 0.5) -> bytes:
    """Write raw bytes to the lab and read until it closes or goes quiet."""
    reader, writer = await asyncio.open_connection("127.0.0.1", lab.port)
    writer.write(data)
    await writer.drain()
    out = b""
    try:
        while chunk := await asyncio.wait_for(reader.read(65536), idle):
            out += chunk
    except TimeoutError:
        pass
    writer.close()
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- 1.1 the lab ---------------------------------------------------------------------------

BENIGN = b"GET /?cb=1 HTTP/1.1\r\nHost: lab\r\n\r\n"


def attack(cl_line: bytes) -> bytes:
    body = b"GET /zz-warden HTTP/1.1\r\nX: X"
    line = cl_line.replace(b"N", str(len(body)).encode())
    return b"POST / HTTP/1.1\r\nHost: lab\r\n" + line + b"\r\n\r\n" + body


@pytest.mark.parametrize(
    ("mode", "line", "third"),
    [
        ("cl0", b"Content-Length: +N", b"404"),
        ("cl0", b"Content-Length: N", b"200"),
        ("pipeline", b"Content-Length: N", b"404"),
        ("patched", b"Content-Length: N", b"200"),
    ],
)
async def test_lab_framing(mode: str, line: bytes, third: bytes) -> None:
    async with Lab(mode) as lab:
        out = await talk(lab, BENIGN + attack(line) + BENIGN)
    statuses = [chunk[:3] for chunk in out.split(b"HTTP/1.1 ")[1:]]
    assert statuses == [b"200", b"200", third], out
    assert (b"zz-warden" in out) == (third == b"404")


async def test_lab_patched_rejects_and_closes() -> None:
    async with Lab("patched") as lab:
        out = await talk(lab, BENIGN + attack(b"Content-Length: +N") + BENIGN)
    assert out.count(b"HTTP/1.1 ") == 2 and b"HTTP/1.1 400" in out


async def test_lab_fold_and_patched_paths() -> None:
    req = b"GET /%DC%8AHost:ca.probe.invalid%DC%8A HTTP/1.1\r\nHost: lab\r\nConnection: close\r\n\r\n"
    async with Lab("fold") as lab:
        assert b"Location: http://ca.probe.invalid/" in await talk(lab, req)
    async with Lab("patched") as lab:
        out = await talk(lab, req)
    assert out.startswith(b"HTTP/1.1 404") and "/܊Host:ca.probe.invalid܊".encode() in out


async def test_lab_erratic() -> None:
    async with Lab("erratic") as lab:
        outs = [
            await talk(lab, BENIGN.replace(b"\r\n\r\n", b"\r\nConnection: close\r\n\r\n")) for _ in range(3)
        ]
    assert [o[9:12] for o in outs] == [b"200", b"503", b"200"]


# --- 1.2 wire --------------------------------------------------------------------------------


def test_parse_two_stacked_answers_and_trailing(desync) -> None:
    h = desync.helpers
    stream = (
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi"
        b"HTTP/1.1 100 Continue\r\n\r\n"
        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n"
        b"HTTP/1.1 200 OK\r\n\r\nextra"
    )
    answers, trailing = h.parse_answers(stream, 2, eof=True)
    assert [a.status for a in answers] == [200, 404] and answers[0].body == b"hi"
    assert trailing == b"HTTP/1.1 200 OK\r\n\r\nextra"
    assert b"".join(a.raw for a in answers) + trailing == stream.replace(
        b"HTTP/1.1 100 Continue\r\n\r\n", b""
    )


def test_parse_chunked_until_close_and_incomplete(desync) -> None:
    h = desync.helpers
    chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n3;x=y\r\nabc\r\n0\r\n\r\n"
    (a,), rest = h.parse_answers(chunked + b"X", 1, eof=False)
    assert a.body == b"helloabc" and rest == b"X"
    assert h.parse_answers(chunked[:-3], 1, eof=False) == ([], b"")  # still arriving
    (cut,), _ = h.parse_answers(chunked[:-3], 1, eof=True)
    assert cut.parse_error == "truncated" and cut.status == 200
    (close,), _ = h.parse_answers(b"HTTP/1.0 200 OK\r\nServer: x\r\n\r\nuntil close", 1, eof=True)
    assert close.body == b"until close" and close.header_names() == {"server"}


def test_parse_errors_keep_the_bytes(desync) -> None:
    h = desync.helpers
    answers, _ = h.parse_answers(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nxGARBAGE IN", 2, eof=True)
    bad = answers[1]
    assert answers[0].body == b"x" and bad.parse_error == "not an HTTP answer" and bad.raw == b"GARBAGE IN"
    (line,), _ = h.parse_answers(b"HTTP/1.1 200 OK\r\nno colon here\r\n\r\n", 1, eof=True)
    assert line.parse_error == "bad header line" and line.raw.startswith(b"HTTP/1.1 200")


async def test_hostile_bytes_reach_the_wire_verbatim(desync) -> None:
    h = desync.helpers
    async with Lab("cl0") as lab:
        t = h.target_of(lab.url + "/")
        raw = b"GET / HTTP/1.1\r\nHost: x\r\nX : +1\r\n\tY: \x8a\xdc\r\nZ:\x0bv\r\nConnection: close\r\n\r\n"
        conn = await h.exchange(t, [raw])
        assert lab.wire() == raw and conn.answer(0).status == 200 and conn.closed_after == 1


async def test_pipelined_answers_are_attributed_in_order(desync) -> None:
    h = desync.helpers
    async with Lab("cl0") as lab:
        t = h.target_of(lab.url + "/")
        benign = h.benign_request(t, "abc")
        body = b"GET /q-warden HTTP/1.1\r\nX: X"
        atk = (
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: +" + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        conn = await h.exchange(t, [benign, atk, benign])
    assert [a.status for a in conn.answers] == [200, 200, 404] and len(lab.received) == 1
    assert conn.closed_after == -1 and not conn.timed_out and b"q-warden" in conn.answer(2).body


async def test_unreachable_and_timeout(desync) -> None:
    h = desync.helpers
    conn = await h.exchange(h.target_of(f"http://127.0.0.1:{free_port()}/"), [b"GET / HTTP/1.1\r\n\r\n"])
    assert conn.error.startswith("connect:") and conn.sent == 0
    async with Lab("cl0") as lab:  # a request with no blank line: the lab waits, we time out
        conn = await h.exchange(h.target_of(lab.url), [b"GET / HTTP/1.1\r\nHost: x\r\n"], timeout=0.3)
    assert conn.timed_out and conn.answers == [] and conn.sent == 1


async def test_pacer_spaces_connections_to_one_host(desync) -> None:
    h = desync.helpers
    pacer = h.Pacer(0.3)
    async with Lab("cl0") as lab:
        t = h.target_of(lab.url + "/")
        req = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"

        async def paced() -> None:
            await pacer.wait(t.host)
            await h.exchange(t, [req])

        await asyncio.gather(paced(), paced(), paced())
    gaps = [b - a for a, b in zip(lab.conn_times, lab.conn_times[1:], strict=False)]
    assert len(lab.conn_times) == 3 and all(g >= 0.28 for g in gaps), gaps


# --- 2.1 smuggling family -------------------------------------------------------------------


def test_default_techniques_are_the_seed_lines(desync) -> None:
    h = desync.helpers
    seeds = h.smuggle_techniques(sweep=False)
    assert sorted(t.line for t in seeds) == sorted(SEED_LINES)
    by_variant = {t.variant: t for t in h.smuggle_family()}
    plus = by_variant["before-value-2b-preserve"]
    assert plus.line == "Content-Length: +${cl}" and plus.tier == 1
    assert plus.provenance == "h1:2444112,h1:2456548,h1:2509057,h1:2509198"
    assert by_variant["before-value-2b-lower"].provenance == "h1:2413017"
    assert by_variant["obs-fold-20-preserve"].line == "Content-Length \r\n : ${cl}"


def test_family_shape(desync) -> None:
    h = desync.helpers
    family = h.smuggle_family()
    assert len(family) == 3630  # 3 casings x 7 positions x 173 bytes, less 3 duplicate spellings
    assert family[:5] == h.smuggle_techniques(sweep=False) and len({t.text for t in family}) == len(family)
    assert not any("\x00" in t.line for t in family)
    assert not any(t.line.lower() == "content-length: ${cl}" for t in family)
    assert {t.variant.split("-")[-1] for t in family} == {"preserve", "lower", "upper"}


def test_attack_and_control_rendering(desync) -> None:
    h = desync.helpers
    for tech in h.smuggle_family()[::97] + h.smuggle_techniques(False):
        atk = h.render_auto_cl(tech.text, host="a.example.com", endpoint="/app", random="r1")
        ctl = h.render_auto_cl(tech.control_text, host="a.example.com", endpoint="/app", random="r1")
        body = b"GET /r1-warden HTTP/1.1\r\nX: X"
        assert atk.endswith(b"\r\n\r\n" + body) and ctl.endswith(b"\r\n\r\n" + body)
        assert h.body_len(atk) == len(body)
        assert f"Content-Length: {len(body)}".encode().lower() in ctl.lower()
        line = tech.line.replace("${cl}", str(len(body))).encode("latin-1")
        ctl_line = tech.control_line.replace("${cl}", str(len(body))).encode("latin-1")
        assert atk.split(line) == ctl.split(ctl_line)  # the control is the attack minus its gadget
        assert b"\r\n\r\n" not in body and b"Host" not in body
    with pytest.raises(ValueError, match="unbound slot"):
        h.render("${tenant}")


# --- 2.2 fold vectors -----------------------------------------------------------------------


def test_tier_one_vectors(desync) -> None:
    h = desync.helpers
    tier1 = h.fold_vectors(1)
    ids = {v.id for v in tier1}
    assert len(tier1) == 31
    long_folds = {v.id for v in tier1 if v.family == "utf8-fold" and len(v.encoded) > 6}
    assert long_folds == {"fold-lf-u0b0a", "fold-lf-u560a", "fold-lf-u1f60a"}
    by_id = {v.id: v for v in tier1}
    assert by_id["fold-lf-u0b0a"].encoded == "%E0%AC%8A" and by_id["fold-lf-u1f60a"].encoded == "%F0%9F%98%8A"
    assert by_id["fold-lf-u070a"].encoded == "%DC%8A" and by_id["fold-lf-u070a"].control == "%DC%8B"
    assert by_id["double1-cr"].encoded == "%250D" and by_id["overlong2-lf"].encoded == "%C0%8A"
    assert {"raw-lf", "raw-cr", "fold-cr-u010d"} <= ids
    assert all(len(v.encoded) == len(v.control) and v.encoded != v.control for v in h.fold_vectors(3))
    assert len(h.fold_vectors(2)) > len(tier1) and len(h.fold_vectors(3)) > len(h.fold_vectors(2))


def test_screen_is_raw_and_first_published_fold(desync) -> None:
    h = desync.helpers
    assert [v.id for v in h.screen_vectors(h.fold_vectors(1))] == [
        "raw-lf",
        "fold-lf-u010a",
        "raw-cr",
        "fold-cr-u010d",
    ]


def test_redaction_is_symmetric_and_body_only(desync) -> None:
    h = desync.helpers
    v = {x.id: x for x in h.fold_vectors(1)}["fold-lf-u070a"]
    fold = "<p>/܊Host:c1.probe.invalid܊ %DC%8A %dc%8a \\u070a</p>\n".encode()
    ctl = "<p>/܋Host:c1.probe.invalid܋ %DC%8B %dc%8b \\u070b</p>\x0b".encode()
    assert h.redact(fold, v) == h.redact(ctl, v)
    assert h.redact(b"same\r\n", v) == b"same\r<V>"


# --- 3.1 oracles -----------------------------------------------------------------------------


def answer(h, status: int, body: bytes = b"x", **kw):
    head = f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    return h.Answer(status=status, head=head, body=body, raw=head + body, **kw)


def conn(h, *answers, trailing=b"", closed_after=-1, error=""):
    return h.Conn(
        answers=list(answers),
        trailing=trailing,
        raw=b"".join(a.raw for a in answers) + trailing,
        closed_after=closed_after,
        error=error,
        sent=3,
    )


def test_stability_gate(desync) -> None:
    h = desync.helpers
    ok = [conn(h, answer(h, 200, b"same")) for _ in range(3)]
    assert h.baseline_of(ok).stable
    erratic = [conn(h, answer(h, s)) for s in (200, 503, 200)]
    assert h.baseline_of(erratic).reason == "erratic: statuses 200,503,200"
    dyn = [conn(h, answer(h, 200, body)) for body in (b"a", b"b", b"a")]
    assert h.baseline_of(dyn).reason == "erratic: status 200, bodies differ"
    down = [h.Conn(error="connect: refused") for _ in range(3)]
    assert h.baseline_of(down).reason == "unreachable: connect: refused"


def test_smuggle_signals(desync) -> None:
    h = desync.helpers
    base = h.baseline_of([conn(h, answer(h, 200, b"home")) for _ in range(3)])
    pre = answer(h, 200, b"home")
    sig, void = h.analyze_smuggle(conn(h, answer(h, 404), answer(h, 200), answer(h, 200, b"home")), base, "k")
    assert sig == [] and void == "pre-flight benign request did not match the baseline"
    rejected = conn(h, pre, answer(h, 400), closed_after=2)
    assert h.analyze_smuggle(rejected, base, "k") == (
        [],
        "",
    )  # a lone close after a rejected attack counts nothing
    cl0 = conn(h, pre, answer(h, 200), answer(h, 404, b"not found: /k-warden"))
    assert h.analyze_smuggle(cl0, base, "k-warden")[0] == [
        "post_differs",
        "post_status_changed",
        "canary_reflected",
    ]
    hung = conn(h, pre, answer(h, 200), closed_after=2)
    assert h.analyze_smuggle(hung, base, "k")[0] == ["post_timeout", "closed_after_attack"]
    dual = conn(h, pre, answer(h, 200), answer(h, 200, b"home"), trailing=b"HTTP/1.1 200")
    assert h.analyze_smuggle(dual, base, "k")[0] == ["dual_response"]
    ctl_quiet = conn(h, pre, answer(h, 200), answer(h, 200, b"home"))
    assert h.control_fired(ctl_quiet, base, "k-warden") is False
    assert h.control_fired(cl0, base, "k-warden") is True
    assert h.control_fired(h.Conn(error="connect: x"), base, "k") is None


def test_split_signals_and_leads(desync) -> None:
    h = desync.helpers
    vs = {v.id: v for v in h.fold_vectors(1)}
    raw = vs["raw-lf"]
    sig, where, void = h.analyze_split(
        conn(h, answer(h, 400)), conn(h, answer(h, 404)), raw, "c1.probe.invalid"
    )
    assert sig == ["status_differs:404->400"] and where == "" and not h.split_lead(raw, sig, where)
    fold = vs["fold-lf-u070a"]
    redirect = answer(h, 301, b"moved")
    redirect.head = redirect.head.replace(b"\r\n\r\n", b"\r\nLocation: http://c1.probe.invalid/\r\n\r\n")
    redirect.headers = [("Location", "http://c1.probe.invalid/")]
    redirect.raw = redirect.head + redirect.body
    echo = answer(h, 404, b"not found: /%DC%8BHost:c1.probe.invalid%DC%8B")
    sig, where, _ = h.analyze_split(conn(h, redirect), conn(h, echo), fold, "c1.probe.invalid")
    assert where == "authority" and sig[0] == "canary_reflected:authority" and "new_headers:location" in sig
    assert h.split_lead(fold, sig, where)
    same = answer(h, 404, b"not found: /%DC%8AHost:c1.probe.invalid%DC%8A")
    assert h.analyze_split(conn(h, same), conn(h, echo), fold, "c1.probe.invalid") == ([], "", "")


def test_printable_and_lead_line(desync) -> None:
    h = desync.helpers
    assert h.printable("\tContent-Length \r\n : 31\x8a\\") == "\\tContent-Length \\r\\n : 31\\x8a\\\\"
    line = h.lead_line(
        "https://a.example.com/",
        "smuggle",
        "before-value-2b-preserve",
        "Content-Length: +${cl}",
        ["post_differs", "canary_reflected"],
    )
    assert line.startswith("https://a.example.com/ [smuggle] [before-value-2b-preserve]")


# --- 3.2 scan_chunk against the lab ---------------------------------------------------------


async def scan(desync, urls: list[str], **kw) -> tuple[dict, dict[str, list[str]]]:
    chunk = desync.activities.ScanChunk(
        run_id="run/test0001/desync-aaaaaa",
        index=1,
        total=1,
        targets=urls,
        rate_ms=kw.pop("rate_ms", 20),
        **kw,
    )
    res = await ActivityEnvironment().run(desync.activities.scan_chunk, chunk)
    files = {
        name: gzip.decompress(base64.b64decode(data)).decode().splitlines()
        for name, data in res.pop("warden_files").items()
    }
    return res, files


def rows(files: dict, check: str) -> list[dict]:
    return [r for r in map(json.loads, files.get("desync.jsonl", [])) if r["check"] == check]


async def test_cl0_target_gives_smuggle_leads(desync) -> None:
    async with Lab("cl0") as lab:
        res, files = await scan(desync, [lab.url + "/"], checks=["smuggle"])
    assert res["probes"] == 5 and res["leads"] == {"smuggle": 5, "split": 0} and res["withdrawn"] == 0
    assert len(files["desync.txt"]) == 5 and "skipped.txt" not in files
    plus = next(line for line in files["desync.txt"] if "[before-value-2b-preserve]" in line)
    assert plus.startswith(f"{lab.url}/ [smuggle] [before-value-2b-preserve] [Content-Length: +${{cl}}]")
    assert "post_differs" in plus and "canary_reflected" in plus
    smug = rows(files, "smuggle")
    assert all(r["control"] == "quiet" and r["lead"] and r["raw_head"] for r in smug)
    assert b"Content-Length\t:\t" in lab.wire() and b"Content-Length \r\n : " in lab.wire()
    gaps = [b - a for a, b in zip(lab.conn_times, lab.conn_times[1:], strict=False)]
    assert min(gaps) >= 0.015  # rate_ms=20 per host


async def test_pipelining_target_is_withdrawn_by_its_control(desync) -> None:
    async with Lab("pipeline") as lab:
        res, files = await scan(desync, [lab.url + "/"], checks=["smuggle"])
    assert res["signalled"] == 5 and res["withdrawn"] == 5 and res["leads"]["smuggle"] == 0
    assert all(
        r["control"] == "fired" and r["signals"] == [] and r["withdrawn_signals"]
        for r in rows(files, "smuggle")
    )
    assert "desync.txt" not in files


async def test_fold_target_gives_split_lead_in_authority(desync) -> None:
    async with Lab("fold") as lab:
        res, files = await scan(desync, [lab.url + "/app"], checks=["split"])
    split = rows(files, "split")
    assert res["probes"] == 31 and {r["phase"] for r in split} == {
        "screen",
        "escalation",
    }  # reacted: whole tier 1
    leads = {r["vector"]: r for r in split if r["lead"]}
    assert (
        leads["fold-lf-u070a"]["reflected_in"] == "authority"
        and leads["fold-cr-u010d"]["reflected_in"] == "authority"
    )
    assert (
        "overlong2-lf" not in leads and "double1-lf" not in leads
    )  # the lab narrows, it does not decode twice
    line = next(x for x in files["desync.txt"] if "[fold-lf-u070a]" in x)
    assert line.startswith(f"{lab.url}/app [split] [fold-lf-u070a] [%DC%8A] [canary_reflected:authority")
    quiet = [r for r in split if not r["signals"]]
    assert quiet and all("raw_head" not in r and r["answers"]["fold"]["digest"] for r in quiet)


async def test_patched_target_gives_nothing(desync) -> None:
    async with Lab("patched") as lab:
        res, files = await scan(desync, [lab.url + "/"])
    assert res["leads"] == {"smuggle": 0, "split": 0} and res["signalled"] == 0
    assert res["probes"] == 5 + 4  # the split screen did not react, so it did not escalate
    assert {r["phase"] for r in rows(files, "split")} == {"screen"}


async def test_erratic_and_unreachable_are_skipped(desync) -> None:
    dead = f"http://127.0.0.1:{free_port()}/"
    async with Lab("erratic") as lab:
        res, files = await scan(desync, [lab.url + "/", dead])
    assert res["probed"] == 0 and res["probes"] == 0 and res["skipped"] == 2
    skipped = dict(line.split(" ", 1) for line in files["skipped.txt"])
    assert skipped[lab.url + "/"] == "erratic: statuses 200,503,200"
    assert skipped[dead].startswith("unreachable: connect:")
    assert len(lab.conn_times) == 3  # the gate only: no probe was sent


async def test_budget_caps_a_url(desync) -> None:
    async with Lab("cl0") as lab:
        res, files = await scan(desync, [lab.url + "/"], sweep=True, budget_s=1, rate_ms=100)
    (line,) = files["skipped.txt"]
    sent, planned = line.split("capped: ")[1].split(" probes")[0].split("/")
    assert line.startswith(lab.url + "/ capped: ") and 0 < int(sent) < int(planned) == 3630 + 31
    assert res["probed"] == 1 and res["skipped"] == 1


# --- 3.3 DesyncScan on a dev server ---------------------------------------------------------


@pytest.fixture
async def desync_worker(dev_server, desync, monkeypatch):
    async with worker(
        dev_server.client,
        desync,
        [desync.workflows.DesyncScan],
        [desync.activities.scan_chunk],
        TQ,
        monkeypatch,
    ):
        yield desync


def test_urls_are_chunked_by_host(desync) -> None:
    urls = [f"https://{h}.example.com/{i}" for i in range(3) for h in ("b", "a")]
    chunks = desync.helpers.chunk_targets(urls + urls[:1], 3)
    assert [{u.split("/")[2] for u in c} for c in chunks] == [{"a.example.com"}, {"b.example.com"}]


async def test_run_assembles_files(desync_worker, dev_server, tmp_path: Path) -> None:
    d, client, rid = desync_worker, dev_server.client, "run/test0001/desync-bbbbbb"
    async with Lab("cl0") as cl0, Lab("patched") as patched:
        urls = [cl0.url + "/", patched.url + "/", cl0.url + "/app", patched.url + "/x"]
        res = await client.execute_workflow(
            d.workflows.DesyncScan.run,
            d.workflows.DesyncInput(targets=urls, checks=["smuggle"], rate_ms=10, chunk_size=2),
            id=rid,
            task_queue=TQ,
        )
    assert res["urls"] == 4 and res["probed"] == 4 and res["probes"] == 20 and res["leads"]["smuggle"] == 10
    assert res["chunks"] == {"total": 2, "succeeded": 2, "failed": 0} and "warden_files" not in res
    files = await collect_files(client, rid, tmp_path)
    leads = Path(files["desync.txt"]["path"]).read_text().splitlines()
    assert len(leads) == 10 and all(line.startswith(cl0.url) for line in leads)
    assert len(Path(files["desync.jsonl"]["path"]).read_text().splitlines()) == 4 + 20


async def test_cancellation_stops_probing(desync_worker, dev_server) -> None:
    d = desync_worker
    async with Lab("cl0") as lab:
        handle = await dev_server.client.start_workflow(
            d.workflows.DesyncScan.run,
            d.workflows.DesyncInput(targets=[lab.url + "/", lab.url + "/app"], sweep=True, rate_ms=50),
            id="run/test0001/desync-cccccc",
            task_queue=TQ,
        )
        for _ in range(100):
            if len(lab.conn_times) > 5:
                break
            await asyncio.sleep(0.05)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        assert (await handle.describe()).status == WorkflowExecutionStatus.CANCELED
        # The activity learns of the cancel at its next heartbeat (0.2 s here, 10 s on a fleet).
        assert await probing_stops(lab)


async def probing_stops(lab: Lab) -> bool:
    """True once no new connection reaches the lab for a second and none is left open (within 10 s)."""
    seen, quiet_since = len(lab.conn_times), time.monotonic()
    while time.monotonic() - quiet_since < 10:
        await asyncio.sleep(0.1)
        if len(lab.conn_times) != seen:
            seen, quiet_since = len(lab.conn_times), time.monotonic()
        elif time.monotonic() - quiet_since >= 1 and lab.open_connections == 0:
            return True
    return False
