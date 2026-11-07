"""Settings loading and required-key errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from warden.config import ConfigError, MissingCredentialsError, load_settings


def test_defaults_without_env() -> None:
    s = load_settings({})
    assert s.temporal_host == "127.0.0.1:7233"
    assert s.modules_dir == Path("modules")
    assert s.results_dir == Path("state/results")
    assert s.hc_bin == "hc"
    assert s.ts_tag == "tag:warden-worker"
    assert s.fleet_max_lifetime_min == 120


def test_require_names_missing_key() -> None:
    s = load_settings({"WARDEN_TAILNET": "me@example.com"})
    with pytest.raises(ConfigError, match="WARDEN_TS_SUFFIX"):
        s.require("tailnet", "ts_suffix")


def test_require_lists_every_missing_key() -> None:
    s = load_settings({})
    with pytest.raises(ConfigError) as exc:
        s.require("tailnet", "ts_suffix", "control_host")
    msg = str(exc.value)
    assert "WARDEN_TAILNET" in msg and "WARDEN_TS_SUFFIX" in msg
    assert "WARDEN_CONTROL_HOST" in msg


def test_blank_values_count_as_missing() -> None:
    s = load_settings({"WARDEN_TAILNET": "   "})
    with pytest.raises(ConfigError, match="WARDEN_TAILNET"):
        s.require("tailnet")


def test_do_token_aliases() -> None:
    assert load_settings({"DO_TOKEN": "t1"}).do_token == "t1"
    assert load_settings({"DIGITALOCEAN_ACCESS_TOKEN": "t2"}).do_token == "t2"
    both = load_settings({"DIGITALOCEAN_TOKEN": "a", "DO_TOKEN": "b"})
    assert both.do_token == "a"


def test_missing_credentials_are_non_retryable_type() -> None:
    s = load_settings({"TAILSCALE_OAUTH_CLIENT_ID": "id"})
    with pytest.raises(MissingCredentialsError, match="TAILSCALE_OAUTH_CLIENT_SECRET"):
        s.require_tailscale_oauth()
    with pytest.raises(MissingCredentialsError, match="DIGITALOCEAN_TOKEN"):
        s.require_do_token()


def test_fleet_temporal_address_derived_from_control_host() -> None:
    s = load_settings({"WARDEN_CONTROL_HOST": "mac.tailnet.ts.net"})
    assert s.fleet_temporal_address == "mac.tailnet.ts.net:7233"
    explicit = load_settings({"WARDEN_CONTROL_HOST": "x", "WARDEN_TEMPORAL_FLEET_ADDRESS": "y.ts.net:7234"})
    assert explicit.fleet_temporal_address == "y.ts.net:7234"
    with pytest.raises(ConfigError, match="WARDEN_CONTROL_HOST"):
        _ = load_settings({}).fleet_temporal_address


def test_modules_and_results_dirs_overridable() -> None:
    s = load_settings({"WARDEN_MODULES_DIR": "~/mods", "WARDEN_RESULTS_DIR": "/tmp/r"})
    assert s.modules_dir == Path("~/mods").expanduser() and s.results_dir == Path("/tmp/r")


def test_tailnet_host() -> None:
    s = load_settings({"WARDEN_TS_SUFFIX": "tail1234.ts.net"})
    assert s.tailnet_host("wf-abcd1234-01") == "wf-abcd1234-01.tail1234.ts.net"


def test_invalid_values_rejected() -> None:
    with pytest.raises(ConfigError, match="WARDEN_READY_TIMEOUT_MIN"):
        load_settings({"WARDEN_READY_TIMEOUT_MIN": "soon"})
    with pytest.raises(ConfigError, match="WARDEN_FLEET_MAX_LIFETIME_MIN"):
        load_settings({"WARDEN_FLEET_MAX_LIFETIME_MIN": "0"})


def test_paths_expand_user() -> None:
    s = load_settings({"WARDEN_KNOWN_HOSTS_FILE": "~/kh"})
    assert s.known_hosts_file == Path("~/kh").expanduser()
