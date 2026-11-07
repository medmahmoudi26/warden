"""Every module folder: a valid entrypoint with a description, and refusal of a download with the wrong checksum."""

from __future__ import annotations

import gzip
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from tests.modules.conftest import MODULES, NAMES
from warden.modules import describe, discover


def test_every_module_is_discovered() -> None:
    assert {m.name for m in discover(MODULES)} == set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_module_folder(name: str) -> None:
    folder = MODULES / name
    entry = folder / "entrypoint.sh"
    assert subprocess.run(["bash", "-n", str(entry)]).returncode == 0
    assert os.access(entry, os.X_OK) and describe(entry)
    assert {"worker.py", "workflows.py", "activities.py", "helpers.py"} <= {p.name for p in folder.iterdir()}


@pytest.mark.parametrize(
    ("name", "url_env", "binary"),
    [("subdomains", "SUBFINDER_URL", "subfinder"), ("webs", "HTTPX_URL", "httpx"), ("nuclei", "NUCLEI_URL", "nuclei"),
     ("ffuf", "FFUF_URL", "ffuf")],
)  # fmt: skip
def test_entrypoint_refuses_a_download_with_the_wrong_checksum(
    tmp_path: Path, name: str, url_env: str, binary: str
) -> None:
    fake = tmp_path / "release"
    if name == "ffuf":
        fake.write_bytes(gzip.compress(b"not the real ffuf"))
    else:
        with zipfile.ZipFile(fake, "w") as z:
            z.writestr(binary, "#!/bin/sh\n")
    bin_dir = tmp_path / "bin"
    r = subprocess.run(
        [str(MODULES / name / "entrypoint.sh"), "--install-only"],
        env={
            **os.environ,
            url_env: fake.as_uri(),
            "WARDEN_BIN_DIR": str(bin_dir),
            "WARDEN_DATA_DIR": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0 and "checksum mismatch" in r.stderr, r.stderr
    assert not (bin_dir / binary).exists()
