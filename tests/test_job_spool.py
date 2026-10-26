"""Tests for quarry.daemon.job_spool — the drain-abort recovery spool."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from quarry.daemon.job_spool import JobSpool, SpoolRecord

if TYPE_CHECKING:
    import pytest

    from quarry.config import Settings


def test_write_creates_a_json_file(tmp_path: Path) -> None:
    """A written record lands as a JSON file carrying its recoverable fields."""
    spool = JobSpool(tmp_path / "spool")
    assert spool.write(SpoolRecord("remember", "mem", "note", "payload text")) is True
    files = list((tmp_path / "spool").glob("remember-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["kind"] == "remember"
    assert data["collection"] == "mem"
    assert data["name"] == "note"
    assert data["payload"] == "payload text"
    assert "spooled_at" in data


def test_spool_dir_and_file_are_private_from_the_start(tmp_path: Path) -> None:
    """The spool dir is 0o700 and the file 0o600 — never group/other-readable.

    The spool holds best-effort-scrubbed content, so on a multi-user host it must
    not be readable by other local users. Perms are set at creation, not by a
    chmod-after (which would leave a world-readable window).
    """
    spool = JobSpool(tmp_path / "spool")
    spool.write(SpoolRecord("remember", "mem", "note", "secret-ish content"))
    spool_dir = tmp_path / "spool"
    assert stat.S_IMODE(spool_dir.stat().st_mode) == 0o700
    spooled = next(spool_dir.glob("remember-*.json"))
    assert stat.S_IMODE(spooled.stat().st_mode) == 0o600


def test_existing_loose_dir_is_tightened(tmp_path: Path) -> None:
    """A pre-existing spool dir left world-readable is re-tightened to 0o700."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir(mode=0o755)
    JobSpool(spool_dir).write(SpoolRecord("ingest", "x.test", "u", "https://x.test"))
    assert stat.S_IMODE(spool_dir.stat().st_mode) == 0o700


def test_fdopen_failure_closes_fd_and_removes_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.fdopen raises, the fd it never adopted is closed and the temp removed."""
    closed: list[int] = []
    real_close = os.close

    def spy_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def boom(*_args: object, **_kwargs: object) -> object:
        msg = "fdopen boom"
        raise OSError(msg)

    monkeypatch.setattr(os, "close", spy_close)
    monkeypatch.setattr(os, "fdopen", boom)
    JobSpool(tmp_path / "spool").write(SpoolRecord("remember", "c", "n", "p"))
    assert closed  # the un-adopted fd was explicitly closed
    assert not list((tmp_path / "spool").glob("*.tmp"))
    assert not list((tmp_path / "spool").glob("*.json"))


def test_write_failure_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the temp is written removes it — no orphaned .tmp lingers."""

    def boom(self: Path, target: Path) -> Path:
        del self, target
        msg = "replace boom"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", boom)
    wrote = JobSpool(tmp_path / "spool").write(SpoolRecord("remember", "c", "n", "p"))
    assert wrote is False  # a failed write reports the loss, never a false success
    assert not list((tmp_path / "spool").glob("*.tmp"))
    assert not list((tmp_path / "spool").glob("*.json"))


def test_write_failure_is_logged_not_raised(tmp_path: Path) -> None:
    """A spool-file failure never wedges shutdown — it is swallowed, not raised."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    # mkdir(parents=True) under a regular file raises OSError; write must absorb it.
    spool = JobSpool(blocker / "spool")
    spool.write(SpoolRecord("remember", "mem", "note", "payload"))
    assert not (blocker / "spool").exists()


def test_for_settings_roots_the_spool_under_quarry_root(tmp_path: Path) -> None:
    """for_settings writes under ``<quarry_root>/spool``."""
    settings = SimpleNamespace(quarry_root=tmp_path)
    spool = JobSpool.for_settings(cast("Settings", settings))
    spool.write(SpoolRecord("ingest", "x.test", "u", "https://x.test/a"))
    assert list((tmp_path / "spool").glob("ingest-*.json"))


def test_distinct_records_get_distinct_filenames(tmp_path: Path) -> None:
    """Two records with the same kind never collide on one filename."""
    spool = JobSpool(tmp_path / "spool")
    spool.write(SpoolRecord("remember", "c", "a", "one"))
    spool.write(SpoolRecord("remember", "c", "b", "two"))
    assert len(list((tmp_path / "spool").glob("remember-*.json"))) == 2


def test_a_failed_write_is_logged_by_class_and_frames_never_its_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spool = JobSpool(tmp_path / "spool")
    record = SpoolRecord(kind="remember", collection="c", name="n", payload="p")
    detail = "/home/me/secret/path denied"

    with (
        patch.object(
            JobSpool, "_atomic_write", side_effect=PermissionError(13, detail)
        ),
        caplog.at_level(logging.ERROR, logger="quarry.daemon.job_spool"),
    ):
        assert spool.write(record) is False

    assert "failed to spool aborted remember job for c" in caplog.text
    assert "PermissionError" in caplog.text
    assert "secret" not in caplog.text


def test_the_file_is_synced_before_the_rename_and_the_directory_after(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def fsync(fd: int) -> None:
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync")
        real_fsync(fd)

    def replace(self: Path, target: Path) -> Path:
        events.append("rename")
        return real_replace(self, target)

    with (
        patch("quarry.private_write.os.fsync", fsync),
        patch.object(Path, "replace", replace),
    ):
        assert JobSpool(tmp_path / "spool").write(
            SpoolRecord("remember", "c", "n", "p")
        )

    # the new spool directory goes into its parent first, then the record's sync
    assert events == ["fsync-dir", "fsync", "rename", "fsync-dir"]


def test_a_failed_file_sync_loses_the_record_cleanly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch("quarry.private_write.os.fsync", side_effect=OSError(errno.EIO, "x")),
        caplog.at_level(logging.ERROR, logger="quarry.daemon.job_spool"),
    ):
        wrote = JobSpool(tmp_path / "spool").write(
            SpoolRecord("remember", "c", "n", "p")
        )

    assert wrote is False
    assert not list((tmp_path / "spool").glob("*"))
    assert "failed to spool aborted remember job for c" in caplog.text


def test_a_failed_directory_sync_is_a_warning_and_the_record_stays(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected")
        real_fsync(fd)

    with (
        patch("quarry.private_write.os.fsync", fsync),
        caplog.at_level(logging.WARNING, logger="quarry.daemon.job_spool"),
    ):
        wrote = JobSpool(tmp_path / "spool").write(
            SpoolRecord("remember", "c", "n", "p")
        )

    assert wrote is True
    assert len(list((tmp_path / "spool").glob("remember-*.json"))) == 1
    assert "directory sync failed" in caplog.text
    assert "EIO" in caplog.text


def test_a_failure_that_is_not_an_oserror_never_raises_into_shutdown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(SpoolRecord, "as_json", side_effect=TypeError("unserialisable")),
        caplog.at_level(logging.ERROR, logger="quarry.daemon.job_spool"),
    ):
        wrote = JobSpool(tmp_path / "spool").write(
            SpoolRecord("remember", "c", "n", "p")
        )

    assert wrote is False
    assert "TypeError" in caplog.text


def test_the_temp_is_removed_when_the_sync_fails_partway(tmp_path: Path) -> None:
    with patch("quarry.private_write.os.fsync", side_effect=RuntimeError("x")):
        JobSpool(tmp_path / "spool").write(SpoolRecord("remember", "c", "n", "p"))

    assert not list((tmp_path / "spool").glob("*.tmp"))


def test_filenames_carry_a_full_uuid_so_no_record_replaces_another() -> None:
    record = SpoolRecord("remember", "c", "n", "p")

    name = record.filename()

    assert len(name.removeprefix("remember-").removesuffix(".json")) == 32
    assert name != record.filename()


def test_a_directory_the_write_creates_is_synced_into_its_parent(
    tmp_path: Path,
) -> None:
    synced: list[int] = []
    real_fsync = os.fsync
    parent_inode = tmp_path.stat().st_ino

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    with patch("quarry.private_write.os.fsync", fsync):
        JobSpool(tmp_path / "spool").write(SpoolRecord("remember", "c", "n", "p"))

    assert parent_inode in synced  # the parent of the spool directory
    assert (tmp_path / "spool").stat().st_ino in synced  # and the spool itself


def test_an_existing_spool_directory_is_not_resynced_into_its_parent(
    tmp_path: Path,
) -> None:
    (tmp_path / "spool").mkdir()
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    with patch("quarry.private_write.os.fsync", fsync):
        JobSpool(tmp_path / "spool").write(SpoolRecord("remember", "c", "n", "p"))

    assert tmp_path.stat().st_ino not in synced
