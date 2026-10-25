"""CaptureFile: owner-only, never through a symlink, durable, and self-cleaning."""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.capture_file import CaptureFile


def _target(tmp_path: Path) -> Path:
    return tmp_path / "captures" / "session-abcd1234.md"


def test_a_capture_is_written_owner_only_whatever_the_umask(tmp_path: Path) -> None:
    target = _target(tmp_path)
    old = os.umask(0)
    try:
        assert CaptureFile(target).write("scrubbed")
    finally:
        os.umask(old)

    assert target.read_text() == "scrubbed"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_an_existing_world_readable_capture_becomes_owner_only(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("old")
    target.chmod(0o644)

    CaptureFile(target).write("new")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_the_temp_file_is_owner_only_from_its_creation(tmp_path: Path) -> None:
    target = _target(tmp_path)
    modes: list[int] = []
    real_fsync = os.fsync

    def inspecting(fd: int) -> None:
        modes.extend(
            stat.S_IMODE(temp.stat().st_mode)
            for temp in target.parent.glob(".session-*.tmp")
        )
        real_fsync(fd)

    old = os.umask(0)
    try:
        with patch("os.fsync", inspecting):
            CaptureFile(target).write("scrubbed")
    finally:
        os.umask(old)

    assert modes
    assert set(modes) == {0o600}


def test_a_symlink_at_the_target_is_refused_and_not_followed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(victim)

    with caplog.at_level(logging.WARNING, logger="quarry.capture_file"):
        landed = CaptureFile(target).write("scrubbed")

    assert not landed
    assert victim.read_text() == "precious"
    assert target.is_symlink()
    assert "symlink" in caplog.text
    assert "session-abcd1234.md" in caplog.text


def test_a_dangling_symlink_is_refused_too(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "nowhere")

    assert not CaptureFile(target).write("scrubbed")
    assert not (tmp_path / "nowhere").exists()


def test_the_directory_is_synced_after_the_rename(tmp_path: Path) -> None:
    target = _target(tmp_path)
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def fsync(fd: int) -> None:
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync")
        real_fsync(fd)

    def replace(self: Path, dest: Path) -> Path:
        events.append("rename")
        return real_replace(self, dest)

    with patch("os.fsync", fsync), patch.object(Path, "replace", replace):
        CaptureFile(target).write("scrubbed")

    assert events == ["fsync", "rename", "fsync-dir"]


def test_a_directory_that_cannot_be_synced_is_a_failure_not_a_silent_success(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "I/O error")
        real_fsync(fd)

    with patch("os.fsync", fsync), pytest.raises(OSError, match="I/O error"):
        CaptureFile(target).write("scrubbed")


def _leftover(directory: Path, name: str, *, age_s: float) -> Path:
    path = directory / name
    path.write_text("half")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def test_stale_temp_files_of_crashed_writes_are_reaped(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    stale = _leftover(target.parent, ".session-feed0001.md.x1y2z3.tmp", age_s=7200)

    CaptureFile(target).write("scrubbed")

    assert not stale.exists()
    assert target.read_text() == "scrubbed"


def test_a_young_temp_file_may_be_a_live_write_and_is_kept(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    young = _leftover(target.parent, ".session-feed0001.md.x1y2z3.tmp", age_s=5)

    CaptureFile(target).write("scrubbed")

    assert young.exists()


def test_only_capture_temp_files_are_reaped(tmp_path: Path) -> None:
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    others = [
        _leftover(target.parent, "notes.tmp", age_s=7200),
        _leftover(target.parent, ".CLAUDE.md.x1y2.tmp", age_s=7200),
        _leftover(target.parent, "session-feed0001.md", age_s=7200),
    ]

    CaptureFile(target).write("scrubbed")

    assert all(path.exists() for path in others)


def test_a_leftover_that_cannot_be_removed_never_fails_the_write(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    _leftover(target.parent, ".session-feed0001.md.x1y2z3.tmp", age_s=7200)

    with patch.object(Path, "unlink", side_effect=PermissionError("not yours")):
        assert CaptureFile(target).write("scrubbed")

    assert target.read_text() == "scrubbed"
