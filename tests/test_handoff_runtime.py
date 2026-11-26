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
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_HANDOFF_RUNTIME = REPO_ROOT / "scripts" / "install-handoff-runtime.sh"


@pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="the Quarry handoff runtime is Linux x86_64 only",
)
@pytest.mark.parametrize(
    "case", ["download", "cached", "cached-no-url", "mismatch", "unpublished"]
)
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
    if case not in {"cached-no-url", "unpublished"}:
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
    if case in {"cached", "cached-no-url"}:
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
    if case in {"download", "cached", "cached-no-url"}:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(installed)
        assert installed.read_bytes() == archive
    else:
        assert result.returncode == 1
        expected = "checksum mismatch" if case == "mismatch" else "No published"
        assert expected in result.stderr
        assert not installed.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="the release job runs on Linux")
@pytest.mark.parametrize(
    "case", ["fresh", "resume", "starter", "wrong-tag", "wrong-digest"]
)
def test_publish_handoff_runtime(tmp_path: Path, case: str) -> None:
    workflow = REPO_ROOT / ".github/workflows/handoff-runtime.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["publish"]["steps"]
    publisher = next(
        step["run"]
        for step in steps
        if step.get("name") == "Publish the verified runtime"
    )
    assets = tmp_path / "runtime-assets"
    assets.mkdir()
    archive = assets / "quarry-handoff-linux-amd64.tar.gz"
    archive.write_bytes(b"published archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = f"{digest}  {archive.name}\n".encode()
    archive.with_suffix(".gz.sha256").write_bytes(checksum)
    gh = r"""
gh() {
  case "$1 $2" in
    'api --method')
      [[ "$CASE" == fresh ]] || return 1
      [[ "$3" == POST && "$4" == "repos/$GITHUB_REPOSITORY/git/refs" &&
         "$5" == -f && "$6" == "ref=refs/tags/$TAG" &&
         "$7" == -f && "$8" == "sha=$GITHUB_SHA" ]] &&
        touch tag-created ;;
    'api repos/punt-labs/quarry/commits/refs/tags/handoff-runtime-v1')
      [[ "$CASE" != fresh ]] || return 99
      printf '%s\n' "$EXISTING_TAG_SHA" ;;
    'release view')
      if [[ "$*" == *isDraft* ]]; then
        [[ "$CASE" != fresh || -e draft-created ]] || return 1
        echo true
      elif [[ "$*" != *tar.gz.sha256* ]]; then
        if [[ -e archive-uploaded ]]; then
          printf 'uploaded\t%s\n' "$ARCHIVE_DIGEST"
        elif [[ "$CASE" != fresh && ! -e starter-deleted ]]; then
          printf '%s\t%s\n' "$EXISTING_STATE" "$EXISTING_DIGEST"
        fi
      elif [[ -e checksum-uploaded ]]; then
        printf 'uploaded\t%s\n' "$CHECKSUM_DIGEST"
      fi ;;
    'release create')
      [[ -e tag-created && "$3" == "$TAG" &&
         "$*" == *'--verify-tag --draft --prerelease --latest=false'* ]] &&
        touch draft-created ;;
    'release delete-asset')
      [[ "$4" == quarry-handoff-linux-amd64.tar.gz && "$EXISTING_STATE" == starter ]] &&
        touch starter-deleted ;;
    'release upload')
      if [[ "$4" == quarry-handoff-linux-amd64.tar.gz ]]; then
        [[ -e starter-deleted || -e draft-created ]] && touch archive-uploaded
      else
        [[ "$4" == quarry-handoff-linux-amd64.tar.gz.sha256 ]] &&
          touch checksum-uploaded
      fi ;;
    'release edit')
      [[ "$3" == "$TAG" && "$*" == *'--draft=false --latest=false'* &&
         -e checksum-uploaded && ( "$CASE" != fresh || -e archive-uploaded ) ]] &&
        touch release-published ;;
    *) return 99 ;;
  esac
}
"""
    existing_digest = "0" * 64 if case == "wrong-digest" else digest
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", gh + publisher],
        cwd=tmp_path,
        env={
            **os.environ,
            "CASE": case,
            "TAG": "handoff-runtime-v1",
            "GITHUB_REPOSITORY": "punt-labs/quarry",
            "GITHUB_SHA": "a" * 40,
            "EXISTING_TAG_SHA": ("b" if case == "wrong-tag" else "a") * 40,
            "EXISTING_STATE": "starter" if case == "starter" else "uploaded",
            "EXISTING_DIGEST": "" if case == "starter" else f"sha256:{existing_digest}",
            "ARCHIVE_DIGEST": f"sha256:{digest}",
            "CHECKSUM_DIGEST": f"sha256:{hashlib.sha256(checksum).hexdigest()}",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert (assets / "tag-created").exists() == (case == "fresh")
    assert (assets / "draft-created").exists() == (case == "fresh")
    assert (assets / "starter-deleted").exists() == (case == "starter")
    assert (assets / "archive-uploaded").exists() == (case in {"fresh", "starter"})
    if case in {"fresh", "resume", "starter"}:
        assert result.returncode == 0, result.stderr
        assert (assets / "checksum-uploaded").exists()
        assert (assets / "release-published").exists()
    else:
        assert result.returncode == 1, result.stderr
        diagnostic = "not the build commit" if case == "wrong-tag" else "differs from"
        assert diagnostic in result.stderr
        assert not (assets / "checksum-uploaded").exists()
        assert not (assets / "release-published").exists()
