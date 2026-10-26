"""EnvFile: the env file is private and durable, and its crashed temps are swept."""

from __future__ import annotations

import errno
import logging
import os
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from quarry.env_file import EnvFile
from quarry.temp_sweep import TempSweep


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


_HEX = "0123456789ab"  # a name EnvFile itself would have made


def _stale(directory: Path, name: str) -> Path:
    return _leftover(directory, name, age_s=TempSweep.STALE_AFTER_S + 60)


def test_planted_stale_leftovers_are_removed_and_named(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stale = [
        _stale(tmp_path, f"quarry.env.{_HEX}.tmp"),
        _stale(tmp_path, "quarry.env.tmp"),
    ]

    with caplog.at_level(logging.WARNING, logger="quarry"):
        _write(tmp_path / "quarry.env")

    assert not any(path.exists() for path in stale)
    [record] = caplog.records
    assert "removed (2)" in record.getMessage()
    assert f"quarry.env.{_HEX}.tmp" in record.getMessage()


def test_a_young_temp_file_is_kept_so_a_live_writer_is_not_hit(
    tmp_path: Path,
) -> None:
    young = _leftover(tmp_path, f"quarry.env.{_HEX}.tmp", age_s=5)

    _write(tmp_path / "quarry.env")

    assert young.exists()


def test_a_temp_dated_far_ahead_of_the_clock_counts_as_stale(tmp_path: Path) -> None:
    future = _leftover(
        tmp_path, f"quarry.env.{_HEX}.tmp", age_s=-(TempSweep.CLOCK_SKEW_S + 600)
    )

    _write(tmp_path / "quarry.env")

    assert not future.exists()


def test_a_temp_a_little_ahead_of_the_clock_is_a_live_writers(tmp_path: Path) -> None:
    ahead = _leftover(
        tmp_path, f"quarry.env.{_HEX}.tmp", age_s=-(TempSweep.CLOCK_SKEW_S - 600)
    )

    _write(tmp_path / "quarry.env")

    assert ahead.exists()


@pytest.mark.parametrize(
    ("age_s", "is_removed"),
    [
        (TempSweep.STALE_AFTER_S - 1, False),
        (TempSweep.STALE_AFTER_S, True),
        (-TempSweep.CLOCK_SKEW_S, False),
        (-TempSweep.CLOCK_SKEW_S - 1, True),
    ],
)
def test_the_staleness_boundaries_on_both_sides(
    tmp_path: Path, age_s: float, *, is_removed: bool
) -> None:
    now = 2_000_000_000.0
    leftover = tmp_path / f"quarry.env.{_HEX}.tmp"
    leftover.write_text("k")
    os.utime(leftover, (now - age_s, now - age_s))

    with patch("quarry.temp_sweep.time.time", return_value=now):
        TempSweep.beside(tmp_path / "quarry.env", "env file").run()

    assert leftover.exists() is not is_removed


def test_a_symlink_that_looks_like_a_leftover_is_refused_not_followed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    victim = tmp_path / "victim"
    victim.write_text("precious")
    link = tmp_path / f"quarry.env.{_HEX}.tmp"
    link.symlink_to(victim)

    with caplog.at_level(logging.WARNING, logger="quarry"):
        _write(tmp_path / "quarry.env")

    assert link.is_symlink()  # left alone
    assert victim.read_text() == "precious"
    assert "non-regular" in caplog.text
    assert link.name in caplog.text


def test_a_directory_named_like_a_temp_is_refused_with_its_own_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    planted = tmp_path / f"quarry.env.{_HEX}.tmp"
    planted.mkdir()

    with caplog.at_level(logging.WARNING, logger="quarry"):
        _write(tmp_path / "quarry.env")

    assert planted.is_dir()
    [record] = [r for r in caplog.records if "non-regular" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert planted.name in record.getMessage()


def test_a_leftover_owned_by_another_user_is_kept_and_counted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    theirs = _stale(tmp_path, f"quarry.env.{_HEX}.tmp")

    with (
        patch("quarry.temp_sweep.os.getuid", return_value=os.getuid() + 1),
        caplog.at_level(logging.WARNING, logger="quarry"),
    ):
        _write(tmp_path / "quarry.env")

    assert theirs.exists()
    assert "foreign-owned, left (1)" in caplog.text
    assert theirs.name in caplog.text


@pytest.mark.parametrize("call", ["lstat", "unlink"])
def test_a_leftover_the_filesystem_refuses_is_counted_with_its_errno(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, call: str
) -> None:
    stuck = _stale(tmp_path, f"quarry.env.{_HEX}.tmp")
    real = getattr(Path, call)

    def refuse(self: Path, *args: object, **kwargs: object) -> object:
        if self == stuck:
            raise PermissionError(errno.EACCES, "injected")
        return real(self, *args, **kwargs)

    with (
        patch.object(Path, call, refuse),
        caplog.at_level(logging.WARNING, logger="quarry"),
    ):
        _write(tmp_path / "quarry.env")

    assert stuck.exists()
    assert "failed (1)" in caplog.text
    assert "EACCES" in caplog.text
    assert (tmp_path / "quarry.env").read_text() == "A=1\n"  # the write went on


@pytest.mark.parametrize("call", ["lstat", "unlink"])
def test_a_leftover_lost_to_a_race_counts_as_neither_removed_nor_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, call: str
) -> None:
    gone = _stale(tmp_path, f"quarry.env.{_HEX}.tmp")
    real = getattr(Path, call)

    def lose(self: Path, *args: object, **kwargs: object) -> object:
        if self == gone:
            raise FileNotFoundError(errno.ENOENT, "raced")
        return real(self, *args, **kwargs)

    with (
        patch.object(Path, call, lose),
        caplog.at_level(logging.WARNING, logger="quarry"),
    ):
        _write(tmp_path / "quarry.env")

    assert caplog.records == []


def test_a_directory_that_cannot_be_listed_is_a_warning_with_its_errno(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch(
            "quarry.temp_sweep.os.scandir",
            side_effect=PermissionError(errno.EACCES, "injected"),
        ),
        caplog.at_level(logging.WARNING, logger="quarry"),
    ):
        _write(tmp_path / "quarry.env")

    assert (tmp_path / "quarry.env").read_text() == "A=1\n"
    assert "could not list" in caplog.text
    assert "EACCES" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "other.env.abc.tmp",
        "quarry.env.bak",
        "quarry.env.tmp.old",
        "quarry.env.short.tmp",
        "quarry.env.0123456789AB.tmp",
        f"quarry.env.{_HEX}0.tmp",
    ],
)
def test_files_that_are_not_names_envfile_makes_are_never_touched(
    tmp_path: Path, name: str
) -> None:
    keeper = _stale(tmp_path, name)

    _write(tmp_path / "quarry.env")

    assert keeper.exists()


def test_a_quiet_sweep_logs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="quarry"):
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
        caplog.at_level(logging.WARNING, logger="quarry"),
    ):
        _write(tmp_path / "quarry.env")

    assert (tmp_path / "quarry.env").read_text() == "A=1\n"
    assert "could not make" in caplog.text


def test_fd_closed_and_tmp_removed_when_fdopen_raises(tmp_path: Path) -> None:
    opened: list[int] = []
    closed: list[int] = []
    real_open, real_close = os.open, os.close

    def recording_open(path: str, flags: int, mode: int = 0o777) -> int:
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
        _write(tmp_path / "quarry.env")

    assert not any(tmp_path.glob("*.tmp"))
    assert len(opened) == 1
    assert opened[0] in closed  # that exact descriptor, not just some close


def test_a_young_leftover_under_the_old_name_is_never_reused(tmp_path: Path) -> None:
    target = tmp_path / "quarry.env"
    leftover = tmp_path / "quarry.env.tmp"
    leftover.write_text("STALE=1\n")

    _write(target)

    assert "STALE" not in target.read_text()
    assert leftover.read_text() == "STALE=1\n"  # untouched, not adopted


def test_two_writes_never_share_a_temp_name(tmp_path: Path) -> None:
    names: list[str] = []
    real_open = os.open

    def recording(path: str, flags: int, mode: int = 0o777, **kw: int) -> int:
        if flags & os.O_EXCL:
            names.append(Path(path).name)
        return real_open(path, flags, mode, **kw)

    with patch("quarry.private_write.os.open", recording):
        _write(tmp_path / "quarry.env")
        _write(tmp_path / "quarry.env")

    assert len(set(names)) == 2
    assert all(name.startswith("quarry.env.") for name in names)


def test_the_config_directory_is_created_owner_only(tmp_path: Path) -> None:
    target = tmp_path / "config" / "quarry.env"
    old = os.umask(0)
    try:
        _write(target)
    finally:
        os.umask(old)

    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_a_failed_directory_sync_is_a_warning_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "quarry.env"
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected")
        real_fsync(fd)

    with (
        patch("quarry.private_write.os.fsync", fsync),
        caplog.at_level(logging.WARNING, logger="quarry"),
    ):
        _write(target)  # does not raise

    assert target.read_text() == "A=1\n"  # the file is in place
    [record] = caplog.records
    assert "directory sync failed (EIO)" in record.getMessage()
    assert "durability not guaranteed" in record.getMessage()


def test_the_temp_file_is_0600_from_creation_whatever_the_umask(
    tmp_path: Path,
) -> None:
    modes: list[int] = []
    real_open = os.open

    def recording(path: str, flags: int, mode: int = 0o777, **kw: int) -> int:
        fd = real_open(path, flags, mode, **kw)
        if flags & os.O_EXCL:
            modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    old = os.umask(0)
    try:
        with patch("quarry.private_write.os.open", recording):
            _write(tmp_path / "quarry.env")
    finally:
        os.umask(old)

    assert modes == [0o600]  # before a byte was written


def test_the_temp_is_created_exclusively_and_never_through_a_symlink(
    tmp_path: Path,
) -> None:
    flags_seen: list[int] = []
    real_open = os.open

    def recording(path: str, flags: int, mode: int = 0o777, **kw: int) -> int:
        flags_seen.append(flags)
        return real_open(path, flags, mode, **kw)

    with patch("quarry.private_write.os.open", recording):
        _write(tmp_path / "quarry.env")

    created = flags_seen[0]
    assert created & os.O_EXCL
    assert created & os.O_NOFOLLOW
    assert created & os.O_CREAT


def test_the_file_is_synced_before_the_rename_and_the_directory_after(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def fsync(fd: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync-{kind}")
        real_fsync(fd)

    def replace(self: Path, target: Path) -> Path:
        events.append("rename")
        return real_replace(self, target)

    with (
        patch("quarry.private_write.os.fsync", fsync),
        patch.object(Path, "replace", replace),
    ):
        _write(tmp_path / "quarry.env")

    assert events == ["fsync-file", "rename", "fsync-dir"]


def test_a_failed_write_removes_the_temp_and_keeps_the_old_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "quarry.env"
    target.write_text("OLD=1\n")

    with (
        patch("quarry.private_write.os.fsync", side_effect=OSError("injected")),
        pytest.raises(OSError, match="injected"),
    ):
        _write(target)

    assert target.read_text() == "OLD=1\n"
    assert not any(tmp_path.glob("*.tmp"))
