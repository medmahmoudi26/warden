"""warden-serve startup checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from warden.serve import StartupError, check_pulumi


def test_check_pulumi_finds_the_cli(tmp_path: Path, monkeypatch) -> None:
    cli = tmp_path / "pulumi"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert check_pulumi() == str(cli)


def test_check_pulumi_fails_fast_without_the_cli(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(StartupError, match="pulumi CLI not found on PATH"):
        check_pulumi()
