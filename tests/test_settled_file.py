"""Tests for quarry.ingestion.settled_file: when a watched file needs no embedding."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from quarry.ingestion.settled_file import SettledFile
from quarry.sync_discovery import FileDiscovery
from quarry.sync_file_store import FileRecord
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[SyncRegistry]:
    conn = SyncRegistry(tmp_path / "registry.db")
    conn.register_directory(tmp_path, "col")
    try:
        yield conn
    finally:
        conn.close()


def _record(path: Path, *, mtime: float | None = None) -> FileRecord:
    stat = path.stat()
    return FileRecord(
        path=str(path),
        collection="col",
        document_name=path.name,
        mtime=stat.st_mtime if mtime is None else mtime,
        size=stat.st_size,
        ingested_at=datetime.now(UTC).isoformat(),
        content_hash=FileDiscovery.content_hash(path),
    )


def test_an_untouched_file_is_current_and_its_row_is_left_alone(
    tmp_path: Path, registry: SyncRegistry
) -> None:
    path = tmp_path / "a.md"
    path.write_text("body")
    record = _record(path)
    registry.files.upsert_file(record)
    assert SettledFile(registry).is_current(record, path)
    assert registry.files.get_file(str(path)) == record


def test_a_bumped_mtime_with_the_same_bytes_is_current_and_restamps_the_row(
    tmp_path: Path, registry: SyncRegistry
) -> None:
    path = tmp_path / "a.md"
    path.write_text("body")
    record = _record(path)
    registry.files.upsert_file(record)
    bumped = record.mtime + 100
    os.utime(path, (bumped, bumped))
    assert SettledFile(registry).is_current(record, path)
    stored = registry.files.get_file(str(path))
    assert stored is not None
    assert stored.mtime == bumped
    assert stored.content_hash == record.content_hash


def test_changed_content_is_not_current_and_the_row_is_left_alone(
    tmp_path: Path, registry: SyncRegistry
) -> None:
    path = tmp_path / "a.md"
    path.write_text("body")
    record = _record(path)
    registry.files.upsert_file(record)
    path.write_text("different and longer body")
    assert not SettledFile(registry).is_current(record, path)
    assert registry.files.get_file(str(path)) == record


def test_a_vanished_file_is_not_current(tmp_path: Path, registry: SyncRegistry) -> None:
    path = tmp_path / "a.md"
    path.write_text("body")
    record = _record(path)
    path.unlink()
    assert not SettledFile(registry).is_current(record, path)


def test_no_record_and_no_file_is_not_current(
    tmp_path: Path, registry: SyncRegistry
) -> None:
    assert not SettledFile(registry).is_current(None, tmp_path / "never.md")


def test_no_record_for_an_existing_file_is_not_current(
    tmp_path: Path, registry: SyncRegistry
) -> None:
    path = tmp_path / "a.md"
    path.write_text("body")
    assert not SettledFile(registry).is_current(None, path)
