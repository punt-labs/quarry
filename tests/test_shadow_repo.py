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

from quarry.shadow._git import GitRunner
from quarry.shadow.config import ShadowConfig
from quarry.shadow.repo import PARENT_TRACKED_REMEDIATION, ShadowRepo, Visibility
from quarry.shadow.rescrub import CaptureReScrubber
from quarry.shadow.sync import CaptureSync

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


class TestStagedCaptures:
    def test_stage_returns_true_on_success(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        (repo.captures_dir / "session-a.md").write_text("clean\n")
        assert repo.stage() is True

    def test_staged_captures_reads_index_blobs(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        (repo.captures_dir / "session-a.md").write_text("hello\n")
        (repo.captures_dir / "notes.txt").write_text("ignored\n")
        repo.stage()
        staged = repo.staged_captures()
        # Only session-*.md is staged (allowlist) and read from the index.
        assert set(staged) == {"session-a.md"}
        assert "hello" in staged["session-a.md"]

    def test_staged_captures_empty_when_nothing_staged(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        assert repo.staged_captures() == {}


def _fail_git_show(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``git show`` exit non-zero while other git/gh calls run for real.

    Simulates the git-level inconsistency where ``ls-files`` reports a staged
    path but ``git show :<rel>`` then cannot read it.
    """
    original = GitRunner.run

    def fake_run(self: GitRunner, argv: list[str]) -> tuple[int, str]:
        if argv[:2] == ["git", "show"]:
            return 1, ""
        return original(self, argv)

    monkeypatch.setattr(GitRunner, "run", fake_run)


class TestStagedBlobFailClosed:
    def test_staged_captures_raises_when_show_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ls-files reports the file (it IS in the index); a failing git show is a
        # git-level inconsistency, so staged_captures raises instead of silently
        # dropping the path from the fixed-point check.
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        (repo.captures_dir / "session-x.md").write_text("clean\n")
        repo.stage()
        _fail_git_show(monkeypatch)
        with pytest.raises(RuntimeError):
            repo.staged_captures()

    def test_unreadable_staged_blob_aborts_capture_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A staged blob git show cannot read must abort the gate fail-closed:
        # no commit, no push — never a silent drop that ships an un-verified blob.
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        (repo.captures_dir / "session-x.md").write_text("clean capture\n")
        repo.stage()
        _fail_git_show(monkeypatch)
        config = ShadowConfig(
            enabled=True,
            remote="git@h:o/r-quarry.git",
            acknowledge_unverified=True,
        )
        sync = CaptureSync(public, config, repo, CaptureReScrubber(repo.captures_dir))
        result = sync.run(fail_open=True)
        assert result.pushed is False
        assert result.committed is False


def _fail_ls_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the capture-enumerating ``git ls-files`` exit non-zero.

    Simulates a git-level enumeration failure (repo corruption, transient
    error) while every other git/gh call runs for real, so an enumeration
    failure never masquerades as "no captures staged".
    """
    original = GitRunner.run

    def fake_run(self: GitRunner, argv: list[str]) -> tuple[int, str]:
        if argv[:3] == ["git", "ls-files", "-z"]:
            return 1, ""
        return original(self, argv)

    monkeypatch.setattr(GitRunner, "run", fake_run)


class TestStagedCapturesEnumerationFailClosed:
    def test_staged_captures_raises_when_ls_files_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failed enumeration is not "no captures": staged blobs may exist that
        # git could not report, so staged_captures raises rather than returning
        # an empty mapping that would pass verification vacuously.
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        (repo.captures_dir / "session-x.md").write_text("clean\n")
        repo.stage()
        _fail_ls_files(monkeypatch)
        with pytest.raises(RuntimeError):
            repo.staged_captures()

    def test_zero_capture_enumeration_returns_empty(self, tmp_path: Path) -> None:
        # ls-files succeeding with no captures (exit 0, empty) is the safe case:
        # an empty index has nothing to verify, so an empty mapping is correct.
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        assert repo.staged_captures() == {}

    def test_enumeration_failure_aborts_capture_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failed enumeration must abort the gate fail-closed: no commit, no
        # push — never a vacuous pass that ships blobs the guard never checked.
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        (repo.captures_dir / "session-x.md").write_text("clean capture\n")
        repo.stage()
        _fail_ls_files(monkeypatch)
        config = ShadowConfig(
            enabled=True,
            remote="git@h:o/r-quarry.git",
            acknowledge_unverified=True,
        )
        sync = CaptureSync(public, config, repo, CaptureReScrubber(repo.captures_dir))
        result = sync.run(fail_open=True)
        assert result.pushed is False
        assert result.committed is False


class TestHasUnpushedCommits:
    def test_local_commit_without_origin_main_is_unpushed(self, tmp_path: Path) -> None:
        # After bootstrap there is no origin/main (never pushed). A local commit
        # must read as UNPUSHED, not "in sync" — the safe direction for a leak.
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        (repo.captures_dir / "session-a.md").write_text("captured\n")
        repo.stage()
        assert repo.commit() is True
        assert repo.has_unpushed_commits() is True

    def test_no_commits_is_not_unpushed(self, tmp_path: Path) -> None:
        public = _public_repo(tmp_path)
        repo = _repo(public)
        repo.bootstrap()
        # Freshly bootstrapped, no commit yet -> nothing to push.
        assert repo.has_unpushed_commits() is False


class TestRemoteVisibility:
    def _completed(
        self, stdout: str, returncode: int = 0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    def test_public_detected(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow._git.subprocess.run",
            return_value=self._completed('{"visibility": "PUBLIC"}'),
        ):
            assert repo.remote_visibility() is Visibility.PUBLIC

    def test_private_detected(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow._git.subprocess.run",
            return_value=self._completed('{"visibility": "private"}'),
        ):
            assert repo.remote_visibility() is Visibility.PRIVATE

    def test_unknown_when_gh_raises(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow._git.subprocess.run",
            side_effect=FileNotFoundError("gh not installed"),
        ):
            assert repo.remote_visibility() is Visibility.UNKNOWN

    def test_unknown_when_gh_nonzero(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        with patch(
            "quarry.shadow._git.subprocess.run",
            return_value=self._completed("", returncode=1),
        ):
            assert repo.remote_visibility() is Visibility.UNKNOWN


class TestGitRunnerNeverRaises:
    def test_invalid_utf8_output_returns_failure(self, tmp_path: Path) -> None:
        # text=True decodes stdout as strict UTF-8 inside subprocess.run, so git
        # output with invalid bytes raises UnicodeDecodeError from within the
        # call. GitRunner promises never to raise: it must return (1, "").
        runner = GitRunner(tmp_path)
        decode_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with patch("quarry.shadow._git.subprocess.run", side_effect=decode_error):
            assert runner.run(["git", "log"]) == (1, "")


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
