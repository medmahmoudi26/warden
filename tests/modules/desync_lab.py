"""A raw-TCP HTTP/1.1 lab with known ground truth, for the desync module's tests.

It reads requests off the socket by hand, so the framing decisions it makes are
exactly the ones under test, and it records every byte and every connection.

Modes:
  cl0       honours a well-formed ``Content-Length: <n>`` line and ignores any other,
            so a malformed one leaves the body in the buffer to prefix the next request
  pipeline  ignores every Content-Length; the smuggling control must withdraw it
  fold      percent-decodes the path, narrows each codepoint to its low byte and splits
            the request there; an injected ``Host:`` line becomes a redirect's authority
  patched   rejects a malformed Content-Length with 400 and closes; 404 pages echo the
            decoded path and nothing is narrowed (the false-positive gate)
  erratic   answers its own benign request with 200, 503, 200, ...

Every mode answers ``ok_paths`` with a fixed 200, other paths with a 404 that names
the requested path.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import time
import urllib.parse

WELL_FORMED_CL = re.compile(rb"^content-length: (\d+)$", re.IGNORECASE)
MODES = ("cl0", "pipeline", "fold", "patched", "erratic")


class Lab:
    def __init__(self, mode: str, ok_paths: tuple[str, ...] = ("/", "/app", "/x")) -> None:
        assert mode in MODES, mode
        self.mode = mode
        self.ok_paths = set(ok_paths)
        self.received: list[bytearray] = []  # per connection, every byte the client sent
        self.conn_times: list[float] = []  # time.monotonic() at each accepted connection
        self.requests: list[bytes] = []  # each request line the lab answered
        self.port = 0
        self._erratic = itertools.cycle((200, 503, 200, 404))
        self._server: asyncio.base_events.Server | None = None
        self._open: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def open_connections(self) -> int:
        return len(self._open)

    async def __aenter__(self) -> Lab:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        for w in list(self._open):
            w.close()
        await self._server.wait_closed()

    def wire(self) -> bytes:
        return b"".join(bytes(r) for r in self.received)

    # --- connection handling ------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.conn_times.append(time.monotonic())
        rec = bytearray()
        self.received.append(rec)
        self._open.add(writer)
        buf = bytearray()
        try:
            while True:
                req = self._take(buf)
                if req is None:
                    data = await asyncio.wait_for(reader.read(65536), 5)
                    if not data:
                        return
                    buf += data
                    rec += data
                    continue
                answer, close = self._answer(*req)
                writer.write(answer)
                await writer.drain()
                if close:
                    return
        except (TimeoutError, ConnectionError):
            return
        finally:
            self._open.discard(writer)
            writer.close()

    def _take(self, buf: bytearray) -> tuple[bytes, list[bytes], bool] | None:
        """The next request (request line, header lines, rejected?) off the buffer, or None."""
        end = buf.find(b"\r\n\r\n")
        if end < 0:
            return None
        lines = bytes(buf[:end]).split(b"\r\n")
        request_line, headers = lines[0], lines[1:]
        length, rejected = 0, False
        for h in headers:
            m = WELL_FORMED_CL.match(h)
            if m and self.mode != "pipeline":
                length = int(m[1])
            elif not m and b"content-length" in h.lower() and self.mode == "patched":
                rejected = True
        if rejected:
            del buf[:]
            return request_line, headers, True
        if len(buf) < end + 4 + length:
            return None
        del buf[: end + 4 + length]
        return request_line, headers, False

    def _answer(self, request_line: bytes, headers: list[bytes], rejected: bool) -> tuple[bytes, bool]:
        self.requests.append(request_line)
        close = any(h.lower() == b"connection: close" for h in headers)
        if rejected:
            return response(400, b"bad request"), True
        parts = request_line.split(b" ")
        if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
            return response(400, b"bad request"), True
        method, target = parts[0], parts[1].split(b"?")[0]
        path = target.decode("latin-1")

        if self.mode == "erratic":
            status = next(self._erratic)
            return response(status, f"status {status}".encode()), close
        if self.mode == "fold":
            narrowed = narrow(path)
            lines = re.split(r"\r\n|\r|\n", narrowed)
            if len(lines) > 1:
                injected = dict(
                    (k.strip().lower(), v.strip())
                    for k, _, v in (line.partition(":") for line in lines[1:])
                    if k
                )
                if "host" in injected:
                    location = f"http://{injected['host']}/"
                    return response(301, b"moved", [("Location", location)]), close
                return response(400, b"bad request"), True
        if path in self.ok_paths and method in (b"GET", b"POST"):
            return response(200, b"hello from the lab" if method == b"GET" else b"ok"), close
        shown = urllib.parse.unquote_to_bytes(path) if self.mode == "patched" else target
        return response(404, b"<html>not found: " + shown + b"</html>"), close


def narrow(path: str) -> str:
    """Percent-decode, decode as UTF-8 (bytes as they are when that fails), keep each codepoint's low byte."""
    raw = urllib.parse.unquote_to_bytes(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return "".join(chr(ord(c) & 0xFF) for c in text)


def response(status: int, body: bytes, headers: list[tuple[str, str]] = ()) -> bytes:
    reason = {200: "OK", 301: "Moved Permanently", 400: "Bad Request", 404: "Not Found", 503: "Unavailable"}
    head = [f"HTTP/1.1 {status} {reason.get(status, 'X')}", "Content-Type: text/html"]
    head += [f"{k}: {v}" for k, v in headers]
    head.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body
