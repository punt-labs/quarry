"""Tests for quarry.daemon.job_spool — the drain-abort recovery spool."""

from __future__ import annotations

import ast
import errno
import fcntl
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from quarry.daemon.job_spool import JobSpool, SpoolRecord

if TYPE_CHECKING:
    from quarry.config import Settings


def test_write_creates_a_json_file(tmp_path: Path) -> None:
    """A written record lands as a JSON file carrying its recoverable fields."""
    spool = JobSpool(tmp_path / "spool")
    written = spool.write(SpoolRecord("remember", "mem", "note", "payload text"))
    files = list((tmp_path / "spool").glob("remember-*.json"))
    assert files == [written]
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
    assert wrote is None  # a failed write reports the loss, never a false success
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
        assert spool.write(record) is None

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

    assert wrote is None
    leftovers = [p for p in (tmp_path / "spool").glob("*") if p.name != ".lock"]
    assert leftovers == []
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

    assert wrote is not None
    assert wrote.exists()
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

    assert wrote is None
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


def test_every_directory_level_the_write_creates_is_synced_top_down(
    tmp_path: Path,
) -> None:
    spool = tmp_path / "a" / "b" / "spool"
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    inodes_before = {tmp_path.stat().st_ino}
    with patch("quarry.private_write.os.fsync", fsync):
        JobSpool(spool).write(SpoolRecord("remember", "c", "n", "p"))

    parents = [tmp_path, tmp_path / "a", tmp_path / "a" / "b"]
    order = [synced.index(p.stat().st_ino) for p in parents]
    assert order == sorted(order)  # top-down
    assert inodes_before <= set(synced)


def test_a_failed_mkdir_sync_does_not_say_the_spool_was_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real_fsync = os.fsync
    calls = {"n": 0}

    def fsync(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # the parent of the new spool directory
            raise OSError(errno.EIO, "injected")
        real_fsync(fd)

    with (
        patch("quarry.private_write.os.fsync", fsync),
        caplog.at_level(logging.WARNING, logger="quarry.private_write"),
    ):
        JobSpool(tmp_path / "spool").write(SpoolRecord("remember", "c", "n", "p"))

    [record] = caplog.records
    assert "spool directory spool created; directory sync failed" in record.getMessage()
    assert "written" not in record.getMessage()


def test_the_startup_sweep_removes_every_temp_young_or_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    stale = spool / ("remember-" + "a" * 32 + ".json.tmp")
    young = spool / ("remember-" + "b" * 32 + ".json.tmp")
    record = spool / ("remember-" + "c" * 32 + ".json")
    for path in (stale, young, record):
        path.write_text("{}")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    with caplog.at_level(logging.INFO, logger="quarry.temp_sweep"):
        JobSpool(spool).sweep()

    assert not stale.exists()
    assert not young.exists()  # no writer is live at startup, so none is young
    assert record.exists()  # a real record is never a leftover
    assert "removed (2)" in caplog.text
    assert "kept (young)" not in caplog.text


def _emitted_kinds() -> set[str]:
    """Return every spool kind the daemon's code can write, read from its source."""
    kinds: set[str] = set()
    for source in Path(__file__).parents[1].joinpath("src/quarry/daemon").rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            keywords = {k.arg: k.value for k in node.keywords}
            candidates: list[ast.expr | None] = []
            if target == "SpoolRecord":
                candidates.append(keywords.get("kind"))
            if target == "ScrubbedIngestJob":
                candidates.append(keywords.get("scrub_label"))
                candidates.append(node.args[-1] if node.args else None)
            kinds.update(
                str(c.value)
                for c in candidates
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            )
    return kinds


def test_every_kind_the_code_emits_is_swept_at_startup(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    kinds = _emitted_kinds()
    assert {"remember", "ingest", "ingest-unrecoverable"} <= kinds  # the parse works
    temps = [spool / f"{kind}-{'d' * 32}.json.tmp" for kind in sorted(kinds)]
    for path in temps:
        path.write_text("{}")

    JobSpool(spool).sweep()

    assert [path.name for path in temps if path.exists()] == []


def test_sweep_of_a_spool_that_does_not_exist_yet_says_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="quarry.temp_sweep"):
        JobSpool(tmp_path / "spool").sweep()

    assert caplog.records == []


def test_a_kind_the_sweep_could_not_match_is_refused_at_once() -> None:
    for bad in ("", "Remember", "re_member", "ingest-", "-ingest", "in gest", "a/b"):
        with pytest.raises(ValueError, match="spool kind"):
            SpoolRecord(bad, "c", "n", "p")


def test_a_write_holding_the_shared_lock_is_never_swept(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    spool.mkdir()
    live = spool / ("remember-" + "a" * 32 + ".json.tmp")
    live.write_text("{}")
    lock = os.open(spool / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock, fcntl.LOCK_SH)  # a writer, mid-write
    sweeping = threading.Thread(target=JobSpool(spool).sweep, daemon=True)
    sweeping.start()
    time.sleep(0.3)

    assert live.exists()  # the sweep is waiting for the writer, not unlinking
    assert sweeping.is_alive()

    os.close(lock)  # the writer is done
    sweeping.join(5)

    assert not sweeping.is_alive()
    assert not live.exists()  # now it is a crash's, and is swept


def test_a_write_waits_for_a_sweep_and_then_lands(tmp_path: Path) -> None:
    spool = JobSpool(tmp_path / "spool")
    assert spool.write(SpoolRecord("remember", "c", "first", "p"))
    exclusive = os.open(tmp_path / "spool" / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(exclusive, fcntl.LOCK_EX)  # a sweep, mid-sweep
    outcome: list[Path | None] = []
    writing = threading.Thread(
        target=lambda: outcome.append(
            spool.write(SpoolRecord("remember", "c", "second", "p"))
        ),
        daemon=True,
    )
    writing.start()
    time.sleep(0.3)

    assert outcome == []  # waiting

    os.close(exclusive)
    writing.join(5)

    assert len(outcome) == 1
    assert outcome[0] is not None
    assert outcome[0].exists()
