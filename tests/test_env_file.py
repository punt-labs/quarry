"""EnvFile: the env file is private and durable, and its crashed temps are swept."""

from __future__ import annotations

import logging
import os
import stat
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.env_file import EnvFile

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _leftover(directory: Path, name: str, *, age_s: float) -> Path:
    path = directory / name
    path.write_text("QUARRY_API_KEY=old\n")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def _write(path: Path) -> None:
    EnvFile(path).write(["A=1"])


def test_the_lines_are_written_one_to_a_line(tmp_path: Path) -> None:
    target = tmp_path / "quarry.env"

    EnvFile(target).write(["A=1", "B=2"])

    assert target.read_text() == "A=1\nB=2\n"


def test_planted_stale_leftovers_are_removed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stale = [
        _leftover(tmp_path, "quarry.env.abc123.tmp", age_s=EnvFile.STALE_AFTER_S + 60),
        _leftover(tmp_path, "quarry.env.tmp", age_s=EnvFile.STALE_AFTER_S + 60),
    ]

    with caplog.at_level(logging.WARNING, logger="quarry.env_file"):
        _write(tmp_path / "quarry.env")

    assert not any(path.exists() for path in stale)
    [record] = caplog.records
    assert "swept 2 leftover" in record.getMessage()


def test_a_young_temp_file_is_kept_so_a_live_writer_is_not_hit(
    tmp_path: Path,
) -> None:
    young = _leftover(tmp_path, "quarry.env.live99.tmp", age_s=5)

    _write(tmp_path / "quarry.env")

    assert young.exists()


def test_a_symlink_that_looks_like_a_leftover_is_refused_not_followed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    link = tmp_path / "quarry.env.evil.tmp"
    link.symlink_to(victim)
    old = time.time() - EnvFile.STALE_AFTER_S - 60
    os.utime(link, (old, old), follow_symlinks=False)

    with caplog.at_level(logging.WARNING, logger="quarry.env_file"):
        _write(tmp_path / "quarry.env")

    assert link.is_symlink()  # left alone
    assert victim.read_text() == "precious"
    assert "left 1 it would not touch" in caplog.text


def test_files_that_are_not_leftovers_are_never_touched(tmp_path: Path) -> None:
    other = _leftover(tmp_path, "other.env.abc.tmp", age_s=EnvFile.STALE_AFTER_S + 60)
    keeper = _leftover(tmp_path, "quarry.env.bak", age_s=EnvFile.STALE_AFTER_S + 60)

    _write(tmp_path / "quarry.env")

    assert other.exists()
    assert keeper.exists()


def test_a_quiet_sweep_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="quarry.env_file"):
        _write(tmp_path / "quarry.env")

    assert caplog.records == []


def test_a_looser_existing_directory_is_tightened(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    config.chmod(0o755)

    _write(config / "quarry.env")

    assert stat.S_IMODE(config.stat().st_mode) == 0o700


def test_a_directory_that_cannot_be_tightened_is_a_warning_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch("pathlib.Path.chmod", side_effect=PermissionError("not mine")),
        caplog.at_level(logging.WARNING, logger="quarry.env_file"),
    ):
        _write(tmp_path / "quarry.env")

    assert (tmp_path / "quarry.env").read_text() == "A=1\n"
    assert "could not make" in caplog.text
