"""Tailscale API client against a fake transport; opt-in live token check."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from warden.config import MissingCredentialsError, load_settings
from warden.tailscale import TailscaleAPI, TailscaleAPIError

SETTINGS = load_settings(
    {
        "TAILSCALE_OAUTH_CLIENT_ID": "cid",
        "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
        "WARDEN_TAILNET": "me@example.com",
    }
)

DEVICES = {
    "devices": [
        {
            "nodeId": "n1",
            "hostname": "wf-abcd1234-01",
            "tags": ["tag:warden-worker"],
            "connectedToControl": True,
            "addresses": ["100.1.1.1"],
        },
        {
            "nodeId": "n2",
            "hostname": "wf-abcd1234-02",
            "tags": ["tag:warden-worker"],
            "connectedToControl": False,
        },
        {
            "nodeId": "n3",
            "hostname": "wf-zzzz9999-01",
            "tags": ["tag:warden-worker"],
            "connectedToControl": True,
        },
        {"nodeId": "n4", "hostname": "my-laptop", "connectedToControl": True},
    ]
}


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.deleted: set[str] = set()

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/oauth/token"):
            assert b"client_id=cid" in body and b"client_secret=sec" in body
            return 200, json.dumps({"access_token": "tok", "expires_in": 3600}).encode()
        assert headers.get("Authorization") == "Bearer tok"
        if method == "GET" and "/devices" in url:
            return 200, json.dumps(DEVICES).encode()
        if method == "DELETE" and "/device/" in url:
            dev = url.rsplit("/", 1)[1]
            if dev in self.deleted or dev == "gone":
                return 404, b'{"message":"not found"}'
            self.deleted.add(dev)
            return 200, b""
        return 500, b'{"message":"unexpected"}'


def test_requires_oauth_credentials() -> None:
    with pytest.raises(MissingCredentialsError, match="TAILSCALE_OAUTH_CLIENT_ID"):
        TailscaleAPI(load_settings({}))


def test_devices_filtered_by_tag_and_prefix() -> None:
    t = FakeTransport()
    api = TailscaleAPI(SETTINGS, transport=t)
    devs = api.devices(tag="tag:warden-worker", hostname_prefix="wf-abcd1234-")
    assert [(d.id, d.hostname, d.online) for d in devs] == [
        ("n1", "wf-abcd1234-01", True),
        ("n2", "wf-abcd1234-02", False),
    ]
    assert "/tailnet/me%40example.com/devices" in t.calls[-1][1]


def test_token_is_cached() -> None:
    t = FakeTransport()
    api = TailscaleAPI(SETTINGS, transport=t)
    api.devices()
    api.devices()
    assert sum(1 for c in t.calls if c[1].endswith("/oauth/token")) == 1


def test_delete_device_idempotent() -> None:
    api = TailscaleAPI(SETTINGS, transport=FakeTransport())
    assert api.delete_device("n1") is True
    assert api.delete_device("n1") is False
    assert api.delete_device("gone") is False


def test_api_errors_raise() -> None:
    def failing(method, url, headers, body):
        return 401, b'{"message":"bad credentials"}'

    with pytest.raises(TailscaleAPIError, match="401"):
        TailscaleAPI(SETTINGS, transport=failing).token()


@pytest.mark.skipif(os.environ.get("WARDEN_TS_IT") != "1", reason="set WARDEN_TS_IT=1 (live Tailscale API)")
def test_live_token_exchange() -> None:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    api = TailscaleAPI(load_settings())
    assert api.token()
    assert isinstance(api.devices(tag="tag:warden-worker"), list)
