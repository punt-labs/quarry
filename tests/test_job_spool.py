"""Tests for quarry.daemon.job_spool — the drain-abort recovery spool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from quarry.daemon.job_spool import JobSpool, SpoolRecord

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings


def test_write_creates_a_json_file(tmp_path: Path) -> None:
    """A written record lands as a JSON file carrying its recoverable fields."""
    spool = JobSpool(tmp_path / "spool")
    spool.write(SpoolRecord("remember", "mem", "note", "payload text"))
    files = list((tmp_path / "spool").glob("remember-*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["kind"] == "remember"
    assert data["collection"] == "mem"
    assert data["name"] == "note"
    assert data["payload"] == "payload text"
    assert "spooled_at" in data


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
