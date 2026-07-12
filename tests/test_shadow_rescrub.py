"""Tests for CaptureReScrubber: the commit-time re-scrub, idempotence, the
I/O-race fixed-point guard, the documented cross-host residual (bug class 1
honesty), and atomic write-back cleanup on failure (bug class 1)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.scrub import Scrubber, ScrubConfig
from quarry.shadow.rescrub import CaptureReScrubber

_LOCAL_HOST = "test-machine"


def _scrubber() -> Scrubber:
    # Pin the local hostname so results do not depend on the CI machine.
    return Scrubber(ScrubConfig(local_hostname=_LOCAL_HOST))


def _seed(dir_: Path, name: str, text: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / name
    path.write_text(text, encoding="utf-8")
    return path


class TestRescrubAll:
    def test_prefpc5_file_rescrubbed_before_commit(self, tmp_path: Path) -> None:
        raw = "user /Users/jim/secret and mail a@b.com on test-machine\n"
        path = _seed(tmp_path, "session-abcd1234.md", raw)
        rescrubber = CaptureReScrubber(tmp_path, _scrubber())

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
        rescrubber = CaptureReScrubber(tmp_path, _scrubber())

        changed = rescrubber.rescrub_all()

        assert changed == 0
        assert path.read_bytes() == before

    def test_only_session_md_processed(self, tmp_path: Path) -> None:
        _seed(tmp_path, "notes.txt", "/Users/jim/x a@b.com\n")
        session = _seed(tmp_path, "session-aa.md", "/Users/jim/x\n")
        rescrubber = CaptureReScrubber(tmp_path, _scrubber())

        rescrubber.rescrub_all()

        # notes.txt is untouched (not a session-*.md); session file is scrubbed.
        assert (tmp_path / "notes.txt").read_text() == "/Users/jim/x a@b.com\n"
        assert "/Users/jim" not in session.read_text(encoding="utf-8")

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        rescrubber = CaptureReScrubber(tmp_path / "absent", _scrubber())
        assert rescrubber.rescrub_all() == 0
        assert rescrubber.verify_clean() == []


class TestVerifyClean:
    def test_fixed_point_file_is_clean(self, tmp_path: Path) -> None:
        _seed(tmp_path, "session-ok.md", "already clean\n")
        rescrubber = CaptureReScrubber(tmp_path, _scrubber())
        assert rescrubber.verify_clean() == []

    def test_non_fixed_point_flagged(self, tmp_path: Path) -> None:
        # A file whose bytes are NOT a scrub fixed point (a stage/commit race).
        path = _seed(tmp_path, "session-race.md", "leak /Users/jim/x\n")
        rescrubber = CaptureReScrubber(tmp_path, _scrubber())
        assert rescrubber.verify_clean() == [path]


class TestResidualLimits:
    def test_cross_host_hostname_not_caught(self, tmp_path: Path) -> None:
        # A capture written on host A, re-scrubbed on host B: A's hostname is a
        # documented residual the scrubber cannot catch. Backstopped only by the
        # private remote — do NOT let a refactor silently "fix" this unnoticed.
        path = _seed(tmp_path, "session-xhost.md", "built on other-host-9\n")
        rescrubber = CaptureReScrubber(tmp_path, _scrubber())
        rescrubber.rescrub_all()
        assert "other-host-9" in path.read_text(encoding="utf-8")


class TestAtomicWriteback:
    def test_cleanup_on_write_failure(self, tmp_path: Path) -> None:
        path = _seed(tmp_path, "session-fail.md", "leak /Users/jim/x\n")
        rescrubber = CaptureReScrubber(tmp_path, _scrubber())

        with (
            patch.object(Path, "write_text", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            rescrubber.rescrub_all()

        # No partial temp file left behind, original preserved.
        assert not (tmp_path / "session-fail.md.tmp").exists()
        assert path.read_text(encoding="utf-8") == "leak /Users/jim/x\n"
