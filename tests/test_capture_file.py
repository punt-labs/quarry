"""CaptureFile: owner-only, never through a symlink at any level, durable."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.artifacts import SessionArtifacts
from quarry.capture import CaptureRequest
from quarry.capture_directory import CaptureDirectory
from quarry.capture_file import CaptureFile
from quarry.capture_outcome import CaptureOutcome
from quarry.capture_temp import CaptureTemp

_NAME = "session-abcd1234.md"
_EMPTY_ARTIFACTS = SessionArtifacts(
    commit_shas=(), pr_numbers=(), branch_names=(), bead_ids=()
)


def _request(root: Path, *, session_id: str = "abcd1234ef") -> CaptureRequest:
    return CaptureRequest(
        project_dir=root,
        session_id=session_id,
        timestamp="2026-07-11T00:00:00Z",
        artifacts=_EMPTY_ARTIFACTS,
        text="the conversation",
        label="pre-compact",
    )


def _write(root: Path, **kwargs: str) -> CaptureOutcome:
    return CaptureFile(_request(root, **kwargs)).write().outcome


def _content(root: Path) -> str:
    return _request(root).scrubbed_content()


def _captures(root: Path) -> Path:
    return root.joinpath(*CaptureDirectory.RELATIVE)


def _victim(tmp_path: Path) -> Path:
    victim = tmp_path / "victim"
    victim.mkdir()
    return victim


def test_a_capture_is_written_owner_only_whatever_the_umask(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        assert _write(tmp_path) is CaptureOutcome.WRITTEN
    finally:
        os.umask(old)

    target = _captures(tmp_path) / _NAME
    assert target.read_text() == _content(tmp_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_an_existing_world_readable_capture_becomes_owner_only(tmp_path: Path) -> None:
    target = _captures(tmp_path) / _NAME
    target.parent.mkdir(parents=True)
    target.write_text("old")
    target.chmod(0o644)

    _write(tmp_path)

    assert target.read_text() == _content(tmp_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_temp_file_is_owner_only_and_locked_while_it_is_written(
    tmp_path: Path,
) -> None:
    seen: list[tuple[int, bool]] = []
    real_fsync = os.fsync

    def inspecting(fd: int) -> None:
        for temp in _captures(tmp_path).glob(".session-*.tmp"):
            probe = os.open(temp, os.O_RDONLY)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = False
            except BlockingIOError:
                locked = True
            finally:
                os.close(probe)
            seen.append((stat.S_IMODE(temp.stat().st_mode), locked))
        real_fsync(fd)

    old = os.umask(0)
    try:
        with patch("os.fsync", inspecting):
            _write(tmp_path)
    finally:
        os.umask(old)

    assert seen
    assert set(seen) == {(0o600, True)}


def test_a_symlink_at_the_target_is_refused_and_not_followed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    victim = _victim(tmp_path) / "precious.txt"
    victim.write_text("precious")
    target = _captures(tmp_path) / _NAME
    target.parent.mkdir(parents=True)
    target.symlink_to(victim)

    with caplog.at_level(logging.WARNING, logger="quarry.capture_file"):
        outcome = _write(tmp_path)

    assert outcome is CaptureOutcome.SYMLINK
    assert victim.read_text() == "precious"
    assert target.is_symlink()
    assert "symlink" in caplog.text
    assert _NAME in caplog.text


def test_a_dangling_symlink_at_the_target_is_refused_too(tmp_path: Path) -> None:
    target = _captures(tmp_path) / _NAME
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "nowhere")

    assert _write(tmp_path) is CaptureOutcome.SYMLINK
    assert not (tmp_path / "nowhere").exists()


def test_a_symlinked_captures_directory_is_refused_and_nothing_lands_outside(
    tmp_path: Path,
) -> None:
    elsewhere = _victim(tmp_path)
    root = tmp_path / "project"
    quarry_dir = root / ".punt-labs" / "quarry"
    quarry_dir.mkdir(parents=True)
    (quarry_dir / "captures").symlink_to(elsewhere)

    outcome = _write(root)

    assert outcome is CaptureOutcome.SYMLINK
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_ancestor_is_refused_and_nothing_lands_outside(
    tmp_path: Path,
) -> None:
    elsewhere = _victim(tmp_path)
    (elsewhere / "quarry" / "captures").mkdir(parents=True)
    root = tmp_path / "project"
    root.mkdir()
    (root / ".punt-labs").symlink_to(elsewhere)

    outcome = _write(root)

    assert outcome is CaptureOutcome.SYMLINK
    assert list((elsewhere / "quarry" / "captures").iterdir()) == []


def test_a_project_root_reached_through_a_symlink_is_fine(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    assert _write(link) is CaptureOutcome.WRITTEN
    assert (_captures(real) / _NAME).read_text() == _content(real)


def test_a_symlink_planted_after_the_check_is_replaced_never_written_through(
    tmp_path: Path,
) -> None:
    victim = _victim(tmp_path) / "precious.txt"
    victim.write_text("precious")
    target = _captures(tmp_path) / _NAME
    real_rename = os.rename

    def plants_then_renames(src: str, dst: str, **kwargs: int) -> None:
        (target.parent / dst).symlink_to(victim)  # the attacker's move, in the gap
        real_rename(src, dst, **kwargs)

    with patch("os.rename", plants_then_renames):
        assert _write(tmp_path) is CaptureOutcome.WRITTEN

    assert victim.read_text() == "precious"
    assert not target.is_symlink()
    assert target.read_text() == _content(tmp_path)


def test_a_directory_swapped_for_a_symlink_after_it_was_opened_is_not_followed(
    tmp_path: Path,
) -> None:
    elsewhere = _victim(tmp_path)
    real_open = CaptureDirectory.open

    def opens_then_swaps(self: CaptureDirectory, *, create: bool) -> int:
        fd = real_open(self, create=create)
        captures = _captures(tmp_path)
        captures.rename(tmp_path / "moved")
        captures.symlink_to(elsewhere)  # the attacker's move, after the open
        return fd

    with patch.object(CaptureDirectory, "open", opens_then_swaps):
        _write(tmp_path)

    assert list(elsewhere.iterdir()) == []  # the write went through the descriptor
    assert (tmp_path / "moved" / _NAME).read_text() == _content(tmp_path)


def test_the_directory_is_synced_after_the_rename(tmp_path: Path) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_rename = os.rename

    def fsync(fd: int) -> None:
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync")
        real_fsync(fd)

    def rename(src: str, dst: str, **kwargs: int) -> None:
        events.append("rename")
        real_rename(src, dst, **kwargs)

    with patch("os.fsync", fsync), patch("os.rename", rename):
        _write(tmp_path)

    assert events == ["fsync", "rename", "fsync-dir"]


def test_a_directory_that_cannot_be_synced_is_a_warning_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "I/O error")
        real_fsync(fd)

    with (
        patch("os.fsync", fsync),
        caplog.at_level(logging.WARNING, logger="quarry.capture_file"),
    ):
        outcome = _write(tmp_path)

    assert outcome is CaptureOutcome.WRITTEN  # the capture is on disk
    assert (_captures(tmp_path) / _NAME).read_text() == _content(tmp_path)
    assert "EIO" in caplog.text


def test_a_write_that_fails_is_a_failed_outcome_and_leaves_no_temp_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    detail = "/home/me/secret no space"

    def fails(_fd: int) -> None:
        raise OSError(errno.ENOSPC, detail)

    with (
        patch("os.fsync", fails),
        caplog.at_level(logging.ERROR, logger="quarry.capture_file"),
    ):
        outcome = _write(tmp_path)

    assert outcome is CaptureOutcome.FAILED
    assert list(_captures(tmp_path).iterdir()) == []
    assert "OSError" in caplog.text
    assert "secret" not in caplog.text  # class and frames, never the message


def test_the_descriptor_is_closed_when_fdopen_raises(tmp_path: Path) -> None:
    opened: list[int] = []
    real_open = os.open

    def recording(
        path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_EXCL:
            opened.append(fd)
        return fd

    with (
        patch("os.open", recording),
        patch("os.fdopen", side_effect=OSError(errno.EMFILE, "too many")),
    ):
        assert _write(tmp_path) is CaptureOutcome.FAILED

    assert len(opened) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[0])
    assert list(_captures(tmp_path).iterdir()) == []


def test_nothing_to_capture_creates_nothing(tmp_path: Path) -> None:
    assert _write(tmp_path, session_id="") is CaptureOutcome.EMPTY
    assert not (tmp_path / ".punt-labs").exists()


def test_a_failed_write_names_the_capture_and_carries_its_cause(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    detail = "/home/me/secret no space"

    def fails(_fd: int) -> None:
        raise OSError(errno.ENOSPC, detail)

    with (
        patch("os.fsync", fails),
        caplog.at_level(logging.ERROR, logger="quarry.capture_file"),
    ):
        result = CaptureFile(_request(tmp_path)).write()

    assert (result.outcome, result.cause) == (CaptureOutcome.FAILED, "ENOSPC")
    assert _NAME in caplog.text  # which capture, so the line is actionable
    assert "ENOSPC" in caplog.text  # the errno the redacted failure names
    assert "secret" not in caplog.text


def test_a_filesystem_that_cannot_lock_still_gets_the_capture(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    CaptureTemp._warn_unlocked.cache_clear()  # warned once per cause, per process

    with (
        patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "no locks")),
        caplog.at_level(logging.WARNING, logger="quarry.capture_temp"),
    ):
        assert _write(tmp_path) is CaptureOutcome.WRITTEN
        assert _write(tmp_path, session_id="feed0001ef") is CaptureOutcome.WRITTEN

    assert (_captures(tmp_path) / _NAME).is_file()
    [warning] = caplog.records  # once, not once per capture
    assert "ENOLCK" in warning.getMessage()
    assert str(_captures(tmp_path)) in warning.getMessage()  # names the directory


def test_an_unexpected_lock_failure_still_fails_the_write(tmp_path: Path) -> None:
    with patch("fcntl.flock", side_effect=OSError(errno.EIO, "io")):
        assert _write(tmp_path) is CaptureOutcome.FAILED

    assert list(_captures(tmp_path).iterdir()) == []


def test_a_temp_file_that_cannot_be_removed_is_a_debug_line_not_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def fails(_fd: int) -> None:
        raise OSError(errno.ENOSPC, "no space")

    with (
        patch("os.fsync", fails),
        patch("os.unlink", side_effect=OSError(errno.EACCES, "denied")),
        caplog.at_level(logging.DEBUG, logger="quarry.capture_temp"),
    ):
        assert _write(tmp_path) is CaptureOutcome.FAILED

    [record] = [r for r in caplog.records if r.name == "quarry.capture_temp"]
    assert record.levelno == logging.DEBUG
    assert "EACCES" in record.getMessage()


def test_a_level_that_is_a_file_is_refused_as_a_non_directory(tmp_path: Path) -> None:
    (tmp_path / ".punt-labs").write_text("a file")

    assert _write(tmp_path) is CaptureOutcome.SYMLINK
    assert CaptureOutcome.SYMLINK.value == (
        "capture refused: symlink or non-directory in path"
    )


def test_an_unlocked_write_is_reported_in_its_result(tmp_path: Path) -> None:
    CaptureTemp._warn_unlocked.cache_clear()
    with patch("fcntl.flock", side_effect=OSError(errno.EOPNOTSUPP, "no locks")):
        unlocked = CaptureFile(_request(tmp_path)).write()
    locked = CaptureFile(_request(tmp_path, session_id="feed0001ef")).write()

    assert (unlocked.outcome, unlocked.unlocked) == (CaptureOutcome.WRITTEN, True)
    assert (locked.outcome, locked.unlocked) == (CaptureOutcome.WRITTEN, False)


def test_a_bad_descriptor_is_a_bug_not_an_unlockable_filesystem(
    tmp_path: Path,
) -> None:
    with patch("fcntl.flock", side_effect=OSError(errno.EBADF, "bad fd")):
        result = CaptureFile(_request(tmp_path)).write()

    assert (result.outcome, result.cause) == (CaptureOutcome.FAILED, "EBADF")
    assert list(_captures(tmp_path).iterdir()) == []
