"""Tests for installing the exact pinned Ethos handoff runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

INSTALL_HANDOFF_RUNTIME = (
    Path(__file__).resolve().parents[1] / "scripts" / "install-handoff-runtime.sh"
)


@pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="the Quarry handoff runtime is Linux x86_64 only",
)
@pytest.mark.parametrize("case", ["download", "cached", "mismatch", "unpublished"])
def test_install_handoff_runtime(tmp_path: Path, case: str) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / INSTALL_HANDOFF_RUNTIME.name
    shutil.copy2(INSTALL_HANDOFF_RUNTIME, script)
    profile = tmp_path / ".punt-labs/ethos/checks/snapshots/quarry-default.yaml"
    profile.parent.mkdir(parents=True)
    archive = b"published runtime bytes"
    digest = hashlib.sha256(archive).hexdigest()
    pin = {"sha256": digest}
    url = (
        "https://github.com/punt-labs/quarry/releases/download/"
        "handoff-runtime-v1/quarry-handoff-linux-amd64.tar.gz"
    )
    if case != "unpublished":
        pin["url"] = url
    profile.write_text(
        json.dumps(
            {"checks": {"full-gate": {"runtime": {"platforms": {"linux-amd64": pin}}}}}
        )
    )
    home = tmp_path / "home"
    home.mkdir()
    store = home / ".punt-labs/ethos/runtimes"
    installed = store / f"{digest}.tar.gz"
    if case == "cached":
        store.mkdir(parents=True)
        installed.write_bytes(archive)
    source = tmp_path / "release.tar.gz"
    source.write_bytes(b"unverified bytes" if case == "mismatch" else archive)
    marker = tmp_path / "curl-called"
    curl = scripts / "curl"
    curl.write_text(
        '#!/bin/sh\nwhile [ "$1" != --output ]; do shift; done\n'
        '[ "$4" = "$RELEASE_URL" ] || exit 2\n'
        ': > "$CURL_MARKER"\ncp "$RELEASE_ARCHIVE" "$2"\n'
    )
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{scripts}:{os.environ['PATH']}",
            "CURL_MARKER": str(marker),
            "RELEASE_ARCHIVE": str(source),
            "RELEASE_URL": url,
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert marker.exists() == (case in {"download", "mismatch"})
    assert not list(store.glob(".quarry-runtime.*"))
    if case in {"download", "cached"}:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(installed)
        assert installed.read_bytes() == archive
    else:
        assert result.returncode == 1
        expected = "checksum mismatch" if case == "mismatch" else "No published"
        assert expected in result.stderr
        assert not installed.exists()
