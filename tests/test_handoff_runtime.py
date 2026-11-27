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
SNAPSHOT = REPO_ROOT / ".punt-labs/ethos/checks/snapshots/quarry-default.yaml"


def _snapshot_check_names() -> list[str]:
    return list(json.loads(SNAPSHOT.read_text())["checks"])


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
    entry = {"runtime": {"platforms": {"linux-amd64": pin}}}
    profile.write_text(
        json.dumps({"checks": dict.fromkeys(_snapshot_check_names(), entry)})
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


def _run_installer(
    tmp_path: Path, snapshot_text: str
) -> subprocess.CompletedProcess[str]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / INSTALL_HANDOFF_RUNTIME.name
    shutil.copy2(INSTALL_HANDOFF_RUNTIME, script)
    profile = tmp_path / ".punt-labs/ethos/checks/snapshots/quarry-default.yaml"
    profile.parent.mkdir(parents=True)
    profile.write_text(snapshot_text)
    home = tmp_path / "home"
    home.mkdir()
    source = tmp_path / "release.tar.gz"
    source.write_bytes(b"unverified bytes")
    curl = scripts / "curl"
    curl.write_text(
        '#!/bin/sh\nwhile [ "$1" != --output ]; do shift; done\n'
        'cp "$RELEASE_ARCHIVE" "$2"\n'
    )
    curl.chmod(0o755)
    return subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{scripts}:{os.environ['PATH']}",
            "RELEASE_ARCHIVE": str(source),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="the Quarry handoff runtime is Linux x86_64 only",
)
def test_installer_reads_the_committed_snapshot(tmp_path: Path) -> None:
    result = _run_installer(tmp_path, SNAPSHOT.read_text())
    assert "Invalid" not in result.stderr, result.stderr
    assert "checksum mismatch" in result.stderr


@pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine != "x86_64",
    reason="the Quarry handoff runtime is Linux x86_64 only",
)
@pytest.mark.parametrize("case", ["differing-pins", "no-handoff-keys"])
def test_installer_rejects_an_inconsistent_snapshot(tmp_path: Path, case: str) -> None:
    checks = json.loads(SNAPSHOT.read_text())["checks"]
    names = list(checks)
    if case == "differing-pins":
        entry = json.loads(json.dumps(checks[names[0]]))
        entry["runtime"]["platforms"]["linux-amd64"]["sha256"] = "0" * 64
        checks[names[0]] = entry
    else:
        checks = {"other-check": checks[names[0]]}
    result = _run_installer(tmp_path, json.dumps({"checks": checks}))
    assert result.returncode == 1
    assert "Invalid Quarry handoff runtime profile" in result.stderr


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
release_assets() {
  if [[ -e archive-uploaded ]]; then
    printf '%s\n' "$ARCHIVE_ASSET"
  elif [[ "$CASE" != fresh && ! -e starter-deleted ]]; then
    printf '%s\n' "$EXISTING_ASSET"
  fi
  if [[ -e checksum-uploaded ]]; then
    printf '%s\n' "$CHECKSUM_ASSET"
  fi
}

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
      if [[ "$7" == isDraft ]]; then
        [[ "$CASE" != fresh || -e draft-created ]] || return 1
        echo true
      else
        [[ "$6" == --json && "$7" == assets && "$8" == --jq ]] || return 99
        release_assets | jq -s '{assets: .}' | jq -r "$9"
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
    existing_digest = f"sha256:{'0' * 64 if case == 'wrong-digest' else digest}"
    existing_state = "starter" if case == "starter" else "uploaded"
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
            "EXISTING_STATE": existing_state,
            "EXISTING_ASSET": json.dumps(
                {
                    "name": archive.name,
                    "state": existing_state,
                    "digest": None if case == "starter" else existing_digest,
                }
            ),
            "ARCHIVE_ASSET": json.dumps(
                {
                    "name": archive.name,
                    "state": "uploaded",
                    "digest": f"sha256:{digest}",
                }
            ),
            "CHECKSUM_ASSET": json.dumps(
                {
                    "name": f"{archive.name}.sha256",
                    "state": "uploaded",
                    "digest": f"sha256:{hashlib.sha256(checksum).hexdigest()}",
                }
            ),
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
