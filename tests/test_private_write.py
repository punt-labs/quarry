"""PrivateWrite: a private file is put in place synced, atomically, and cleaned up."""

from __future__ import annotations

import errno
import logging
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.private_write import PrivateWrite


def test_the_text_lands_in_a_private_file_and_the_temp_is_gone(tmp_path: Path) -> None:
    target, tmp = tmp_path / "f", tmp_path / "f.tmp"

    PrivateWrite("thing").commit(tmp, target, "héllo\n")

    assert target.read_text(encoding="utf-8") == "héllo\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not tmp.exists()


def test_a_temp_that_already_exists_is_never_written_through(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    tmp = tmp_path / "f.tmp"
    tmp.symlink_to(victim)

    with pytest.raises(FileExistsError):
        PrivateWrite("thing").commit(tmp, tmp_path / "f", "x")

    assert victim.read_text() == "precious"


def test_a_failure_before_the_rename_removes_the_temp_and_keeps_the_old_file(
    tmp_path: Path,
) -> None:
    target, tmp = tmp_path / "f", tmp_path / "f.tmp"
    target.write_text("old")

    with (
        patch("quarry.private_write.os.fsync", side_effect=OSError(errno.EIO, "x")),
        pytest.raises(OSError, match="x"),
    ):
        PrivateWrite("thing").commit(tmp, target, "new")

    assert target.read_text() == "old"
    assert not tmp.exists()


def test_a_directory_that_cannot_be_synced_is_a_warning_naming_the_label(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected")
        real_fsync(fd)

    with (
        patch("quarry.private_write.os.fsync", fsync),
        caplog.at_level(logging.WARNING, logger="quarry.private_write"),
    ):
        PrivateWrite("the thing").commit(tmp_path / "f.tmp", tmp_path / "f", "x")

    assert (tmp_path / "f").read_text() == "x"
    [record] = caplog.records
    assert "the thing written; directory sync failed (EIO)" in record.getMessage()


def test_a_temp_that_cannot_be_removed_is_a_warning_and_the_real_error_surfaces(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real_unlink = Path.unlink

    def refuse(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == "f.tmp":
            raise PermissionError(errno.EACCES, "injected")
        real_unlink(self, missing_ok=missing_ok)

    def broken_replace(self: Path, target: Path) -> Path:
        raise OSError(errno.EIO, "rename refused")

    with (
        patch.object(Path, "replace", broken_replace),
        patch.object(Path, "unlink", refuse),
        caplog.at_level(logging.WARNING, logger="quarry.private_write"),
        pytest.raises(OSError, match="rename refused"),
    ):
        PrivateWrite("the thing").commit(tmp_path / "f.tmp", tmp_path / "f", "x")

    [record] = caplog.records
    assert "the thing: could not remove its temp file (EACCES)" in record.getMessage()


def test_when_fdopen_raises_the_fd_is_closed_and_the_temp_removed(
    tmp_path: Path,
) -> None:
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def recording_open(path: Path, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        if flags & os.O_EXCL:
            opened.append(fd)
        return fd

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    with (
        patch("quarry.private_write.os.open", recording_open),
        patch("quarry.private_write.os.fdopen", side_effect=OSError("injected")),
        patch("quarry.private_write.os.close", recording_close),
        pytest.raises(OSError, match="injected"),
    ):
        PrivateWrite("thing").commit(tmp_path / "f.tmp", tmp_path / "f", "x")

    assert len(opened) == 1
    assert opened[0] in closed
    assert not list(tmp_path.iterdir())


def test_when_the_rename_fails_the_temp_is_removed(tmp_path: Path) -> None:
    def broken_replace(self: Path, target: Path) -> Path:
        raise OSError(errno.EIO, "rename refused")

    with (
        patch.object(Path, "replace", broken_replace),
        pytest.raises(OSError, match="rename refused"),
    ):
        PrivateWrite("thing").commit(tmp_path / "f.tmp", tmp_path / "f", "x")

    assert not list(tmp_path.iterdir())


def test_the_temp_is_0600_from_creation_whatever_the_umask(tmp_path: Path) -> None:
    modes: list[int] = []
    real_open = os.open

    def recording_open(path: Path, flags: int, mode: int = 0o777) -> int:
        fd = real_open(path, flags, mode)
        if flags & os.O_EXCL:
            modes.append(stat.S_IMODE(os.fstat(fd).st_mode))  # before any chmod
        return fd

    old = os.umask(0)
    try:
        with patch("quarry.private_write.os.open", recording_open):
            PrivateWrite("thing").commit(tmp_path / "f.tmp", tmp_path / "f", "x")
    finally:
        os.umask(old)

    assert modes == [0o600]
