"""Minimal Tailscale API client (OAuth client credentials).

Used for fleet readiness (is a device online?) and teardown (delete devices
immediately instead of waiting for ephemeral cleanup). Code only needs the
``auth_keys`` and ``devices:core`` scopes.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from warden.config import Settings

API = "https://api.tailscale.com/api/v2"

# (method, url, headers, body) -> (status, body bytes)
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class TailscaleAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"tailscale api {status}: {message}")
        self.status = status


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass
class Device:
    id: str
    hostname: str
    tags: list[str]
    online: bool
    addresses: list[str]


class TailscaleAPI:
    def __init__(self, settings: Settings, transport: Transport | None = None) -> None:
        self.client_id, self.client_secret = settings.require_tailscale_oauth()
        self.tailnet = settings.tailnet or "-"
        self._transport = transport or _urllib_transport
        self._token: str | None = None
        self._token_exp = 0.0

    # -- auth --

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        body = urllib.parse.urlencode(
            {"client_id": self.client_id, "client_secret": self.client_secret}
        ).encode()
        status, raw = self._transport(
            "POST",
            f"{API}/oauth/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        )
        data = self._json(status, raw)
        self._token = data["access_token"]
        self._token_exp = time.time() + float(data.get("expires_in", 3600))
        return self._token

    def _json(self, status: int, raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {"message": raw[:200].decode(errors="replace")}
        if status >= 400:
            raise TailscaleAPIError(status, str(data.get("message", "")))
        return data

    def _call(self, method: str, path: str) -> tuple[int, dict[str, Any]]:
        status, raw = self._transport(
            method, f"{API}{path}", {"Authorization": f"Bearer {self.token()}"}, None
        )
        if status == 404 and method == "DELETE":
            return status, {}
        return status, self._json(status, raw)

    # -- devices --

    def devices(self, *, tag: str | None = None, hostname_prefix: str = "") -> list[Device]:
        tailnet = urllib.parse.quote(self.tailnet, safe="-")
        _, data = self._call("GET", f"/tailnet/{tailnet}/devices?fields=all")
        out: list[Device] = []
        for d in data.get("devices", []):
            tags = list(d.get("tags") or [])
            hostname = str(d.get("hostname", ""))
            if tag and tag not in tags:
                continue
            if hostname_prefix and not hostname.startswith(hostname_prefix):
                continue
            out.append(
                Device(
                    id=str(d.get("nodeId") or d.get("id")),
                    hostname=hostname,
                    tags=tags,
                    online=bool(d.get("connectedToControl")),
                    addresses=list(d.get("addresses") or []),
                )
            )
        return out

    def delete_device(self, device_id: str) -> bool:
        """Delete a device. Returns False if it was already gone."""
        status, _ = self._call("DELETE", f"/device/{urllib.parse.quote(device_id)}")
        return status != 404
