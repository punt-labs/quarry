"""Tests for ShadowRepo: the allowlist .gitignore (bug class 1), the nested repo
staying invisible to the parent, the two fail-closed bootstrap gates including
refuse-parent-tracked with the history-purge remediation (bug class 5 / B3), and
visibility resolution via gh (bug class 4)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from quarry.shadow.repo import PARENT_TRACKED_REMEDIATION, ShadowRepo, Visibility

if TYPE_CHECKING:
    from collections.abc import Iterator

_CAPTURES = (".punt-labs", "quarry", "captures")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.com")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _public_repo(tmp_path: Path, *, ignore_captures: bool = True) -> Path:
    """Init a public repo; gitignore its captures dir unless asked otherwise."""
    repo = tmp_path / "public"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("public repo\n")
    if ignore_captures:
        (repo / ".gitignore").write_text(".punt-labs/quarry/captures/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _repo(public: Path, remote: str = "git@h:o/r-quarry.git") -> ShadowRepo:
    captures = public.joinpath(*_CAPTURES)
    return ShadowRepo(captures, public, remote)


def _tracked_files(captures: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=captures, capture_output=True, text=True, check=True
    )
    return sorted(out.stdout.splitlines())


class TestBootstrapGates:
    def test_refuses_when_not_ignored(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path, ignore_captures=False)
        repo = _repo(public)
        assert repo.bootstrap() is False
        assert not repo.captures_dir.joinpath(".git").exists()

    def test_refuses_parent_tracked_captures(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A capture committed into the public repo BEFORE the gitignore rule.
        public = tmp_path / "public"
        public.mkdir()
        _git(public, "init", "-b", "main")
        captures = public.joinpath(*_CAPTURES)
        captures.mkdir(parents=True)
        (captures / "session-old.md").write_text("leaked capture\n")
        _git(public, "add", "-A")
        _git(public, "commit", "-m", "committed a capture")
        # Now add the gitignore (does NOT untrack the already-committed file).
        (public / ".gitignore").write_text(".punt-labs/quarry/captures/\n")
        _git(public, "add", ".gitignore")
        _git(public, "commit", "-m", "ignore captures")

        repo = _repo(public)
        tracked = repo.parent_tracked_captures()
        assert any("session-old.md" in str(p) for p in tracked)
        with caplog.at_level("WARNING"):
            assert repo.bootstrap() is False
        assert not captures.joinpath(".git").exists()
        # Remediation must name both git rm --cached AND the history purge caveat.
        assert "git rm --cached" in PARENT_TRACKED_REMEDIATION
        assert "filter-repo" in PARENT_TRACKED_REMEDIATION
        assert "force-push" in PARENT_TRACKED_REMEDIATION

    def test_refuses_when_no_remote(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public, remote="")
        # No configured remote and no git origin to derive from -> refuse.
        assert repo.bootstrap() is False


class TestBootstrapCreatesNestedRepo:
    def test_bootstrap_absent_inits_and_allowlists(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public)
        assert repo.bootstrap() is True
        assert repo.captures_dir.joinpath(".git").exists()
        gitignore = repo.captures_dir.joinpath(".gitignore").read_text()
        assert gitignore == "*\n!.gitignore\n!session-*.md\n"

    def test_nested_git_invisible_to_parent(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=public,
            capture_output=True,
            text=True,
            check=True,
        )
        # The parent never sees the captures dir, and no gitlink/.gitmodules.
        assert "captures" not in status.stdout
        assert not (public / ".gitmodules").exists()

    def test_allowlist_excludes_non_md(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        captures = repo.captures_dir
        (captures / "notes.txt").write_text("stray PII /Users/jim/x\n")
        (captures / "debug.log").write_text("more PII a@b.com\n")
        (captures / "session-keep.md").write_text("kept\n")

        repo.stage()
        repo.commit()

        tracked = _tracked_files(captures)
        assert tracked == [".gitignore", "session-keep.md"]


class TestRemoteVisibility:
    def _completed(
        self, stdout: str, returncode: int = 0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    def test_public_detected(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow.repo.subprocess.run",
            return_value=self._completed('{"visibility": "PUBLIC"}'),
        ):
            assert repo.remote_visibility() is Visibility.PUBLIC

    def test_private_detected(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow.repo.subprocess.run",
            return_value=self._completed('{"visibility": "private"}'),
        ):
            assert repo.remote_visibility() is Visibility.PRIVATE

    def test_unknown_when_gh_raises(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow.repo.subprocess.run",
            side_effect=FileNotFoundError("gh not installed"),
        ):
            assert repo.remote_visibility() is Visibility.UNKNOWN

    def test_unknown_when_gh_nonzero(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow.repo.subprocess.run",
            return_value=self._completed("", returncode=1),
        ):
            assert repo.remote_visibility() is Visibility.UNKNOWN


class TestVisibilityEnum:
    def test_from_gh_mapping(self) -> None:
        assert Visibility.from_gh("PUBLIC") is Visibility.PUBLIC
        assert Visibility.from_gh("private") is Visibility.PRIVATE
        assert Visibility.from_gh("internal") is Visibility.UNKNOWN


class TestOwnerRepo:
    def test_ssh_and_https(self) -> None:
        assert ShadowRepo._owner_repo("git@github.com:org/repo-quarry.git") == (
            "org/repo-quarry"
        )
        assert ShadowRepo._owner_repo("https://github.com/org/repo-quarry.git") == (
            "org/repo-quarry"
        )


def _unused(_it: Iterator[None]) -> None:  # pragma: no cover - typing anchor
    pass
