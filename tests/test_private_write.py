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
