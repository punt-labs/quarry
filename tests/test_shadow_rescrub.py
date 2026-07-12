"""Tests for CaptureReScrubber: the commit-time re-scrub, idempotence, the
I/O-race fixed-point guard, the documented cross-host residual (bug class 1
honesty), and atomic write-back cleanup on failure (bug class 1)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.scrub import Scrubber, ScrubConfig
from quarry.shadow.repo import ShadowRepo
from quarry.shadow.rescrub import CaptureReScrubber

_LOCAL_HOST = "test-machine"
_CAPTURES = (".punt-labs", "quarry", "captures")


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.com")
    # CaptureReScrubber builds its own default Scrubber, which resolves the
    # local hostname via socket.gethostname(); pin it so scrub results do not
    # depend on the CI machine's name.
    monkeypatch.setattr("quarry.scrub.socket.gethostname", lambda: _LOCAL_HOST)


def _scrubber() -> Scrubber:
    # Pin the local hostname so direct scrub assertions match the rescrubber's.
    return Scrubber(ScrubConfig(local_hostname=_LOCAL_HOST))


def _seed(dir_: Path, name: str, text: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / name
    path.write_text(text, encoding="utf-8")
    return path


def _init_captures_repo(tmp_path: Path) -> ShadowRepo:
    """Build a bootstrapped ShadowRepo whose captures dir is a nested git repo."""
    public = tmp_path / "public"
    public.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=public, check=True, capture_output=True
    )
    (public / ".gitignore").write_text(".punt-labs/quarry/captures/\n")
    captures = public.joinpath(*_CAPTURES)
    repo = ShadowRepo(captures, public, "git@h:o/r-quarry.git")
    assert repo.bootstrap() is True
    return repo


class TestRescrubAll:
    def test_prefpc5_file_rescrubbed_before_commit(self, tmp_path: Path) -> None:
        raw = "user /Users/jim/secret and mail a@b.com on test-machine\n"
        path = _seed(tmp_path, "session-abcd1234.md", raw)
        rescrubber = CaptureReScrubber(tmp_path)

        changed = rescrubber.rescrub_all()

        assert changed == 1
        scrubbed = path.read_text(encoding="utf-8")
        assert "/Users/jim" not in scrubbed
        assert "a@b.com" not in scrubbed
        assert "test-machine" not in scrubbed
        # The file is now a scrub fixed point.
        assert _scrubber().scrub(scrubbed)[0] == scrubbed

    def test_already_redacted_is_byte_noop(self, tmp_path: Path) -> None:
        raw = "clean content, nothing to redact here\n"
        path = _seed(tmp_path, "session-clean000.md", raw)
        before = path.read_bytes()
        rescrubber = CaptureReScrubber(tmp_path)

        changed = rescrubber.rescrub_all()

        assert changed == 0
        assert path.read_bytes() == before

    def test_only_session_md_processed(self, tmp_path: Path) -> None:
        _seed(tmp_path, "notes.txt", "/Users/jim/x a@b.com\n")
        session = _seed(tmp_path, "session-aa.md", "/Users/jim/x\n")
        rescrubber = CaptureReScrubber(tmp_path)

        rescrubber.rescrub_all()

        # notes.txt is untouched (not a session-*.md); session file is scrubbed.
        assert (tmp_path / "notes.txt").read_text() == "/Users/jim/x a@b.com\n"
        assert "/Users/jim" not in session.read_text(encoding="utf-8")

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        rescrubber = CaptureReScrubber(tmp_path / "absent")
        assert rescrubber.rescrub_all() == 0
        assert rescrubber.verify_staged_clean({}) == []


class TestVerifyStagedClean:
    def test_fixed_point_blob_is_clean(self, tmp_path: Path) -> None:
        rescrubber = CaptureReScrubber(tmp_path)
        staged = {"session-ok.md": "already clean\n"}
        assert rescrubber.verify_staged_clean(staged) == []

    def test_non_fixed_point_blob_flagged(self, tmp_path: Path) -> None:
        # A STAGED blob whose bytes are NOT a scrub fixed point (a re-stage race
        # that left the index unscrubbed). Flagged by relpath, not disk path.
        rescrubber = CaptureReScrubber(tmp_path)
        staged = {"session-race.md": "leak /Users/jim/x\n"}
        assert rescrubber.verify_staged_clean(staged) == [Path("session-race.md")]

    def test_staged_blob_caught_when_working_tree_is_clean(
        self, tmp_path: Path
    ) -> None:
        # The core FIX-1 gap: the INDEX holds an unscrubbed blob while the disk
        # reads clean. A working-tree check would pass; the staged check must
        # catch it. Stage unscrubbed content, then overwrite the file on disk
        # WITHOUT re-staging, so index != working tree.
        repo = _init_captures_repo(tmp_path)
        captures = repo.captures_dir
        (captures / "session-x.md").write_text("leak /Users/jim/x\n", encoding="utf-8")
        repo.stage()  # index now holds the unscrubbed blob
        (captures / "session-x.md").write_text("clean\n", encoding="utf-8")

        rescrubber = CaptureReScrubber(captures)
        staged = repo.staged_captures()
        offenders = rescrubber.verify_staged_clean(staged)

        assert offenders == [Path("session-x.md")]


class TestResidualLimits:
    def test_cross_host_hostname_not_caught(self, tmp_path: Path) -> None:
        # A capture written on host A, re-scrubbed on host B: A's hostname is a
        # documented residual the scrubber cannot catch. Backstopped only by the
        # private remote — do NOT let a refactor silently "fix" this unnoticed.
        path = _seed(tmp_path, "session-xhost.md", "built on other-host-9\n")
        rescrubber = CaptureReScrubber(tmp_path)
        rescrubber.rescrub_all()
        assert "other-host-9" in path.read_text(encoding="utf-8")


class TestAtomicWriteback:
    def test_cleanup_on_write_failure(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, "session-fail.md", "leak /Users/jim/x\n")
        rescrubber = CaptureReScrubber(tmp_path)

        with (
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            rescrubber.rescrub_all()

        # No partial temp file left behind, original preserved.
        assert not (tmp_path / "session-fail.md.tmp").exists()
        assert path.read_text(encoding="utf-8") == "leak /Users/jim/x\n"
