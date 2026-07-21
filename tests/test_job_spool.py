"""Tests for quarry.daemon.job_spool — the drain-abort recovery spool."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

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
