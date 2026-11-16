"""``TableProbe`` measures the whole database, chunks and pages, without row data."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import lancedb
import pytest

from quarry.db.storage import get_db
from quarry.db.table_probe import TableProbe

_ROW = {"id": 1, "text": "hello" * 200}


def _tables(db_path: Path, *names: str) -> None:
    db = lancedb.connect(str(db_path))
    for name in names:
        db.create_table(name, [_ROW] * 20)


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def test_pages_table_counts_toward_disk_bytes(tmp_path: Path) -> None:
    _tables(tmp_path, "chunks", "pages")

    sizes = TableProbe.sizes(tmp_path)

    assert sizes.disk_bytes == _dir_bytes(tmp_path / "chunks.lance") + _dir_bytes(
        tmp_path / "pages.lance"
    )


def test_pages_table_counts_toward_live_bytes(tmp_path: Path) -> None:
    _tables(tmp_path, "chunks")
    chunks_only = TableProbe.sizes(tmp_path).live_bytes
    _tables(tmp_path, "pages")

    assert TableProbe.sizes(tmp_path).live_bytes > chunks_only


def test_chunks_alone_still_measures(tmp_path: Path) -> None:
    _tables(tmp_path, "chunks")

    assert TableProbe.sizes(tmp_path).disk_bytes == _dir_bytes(
        tmp_path / "chunks.lance"
    )


def test_no_tables_measures_zero(tmp_path: Path) -> None:
    assert TableProbe.sizes(tmp_path).disk_bytes == 0


def test_file_deleted_mid_walk_does_not_abort_the_measurement(tmp_path: Path) -> None:
    _tables(tmp_path, "chunks")
    table_dir = tmp_path / "chunks.lance"
    expected = _dir_bytes(table_dir)
    listed = [*table_dir.rglob("*"), table_dir / "compacted-away.lance"]
    real_stat = Path.stat

    def racing_stat(self: Path, **kwargs: bool) -> os.stat_result:
        if self.name == "compacted-away.lance":
            raise FileNotFoundError(str(self))
        return real_stat(self, **kwargs)

    with (
        patch.object(Path, "rglob", lambda _self, _pattern: iter(listed)),
        patch.object(Path, "stat", racing_stat),
    ):
        assert TableProbe.sizes(tmp_path).disk_bytes == expected


def test_other_os_errors_still_propagate(tmp_path: Path) -> None:
    _tables(tmp_path, "chunks")

    with (
        patch.object(Path, "stat", side_effect=PermissionError("denied")),
        pytest.raises(PermissionError),
    ):
        TableProbe.sizes(tmp_path)


def test_passed_connection_is_used_and_no_new_one_opened(tmp_path: Path) -> None:
    _tables(tmp_path, "chunks")
    db = get_db(tmp_path)

    with patch("lancedb.connect") as connect:
        sizes = TableProbe.sizes(tmp_path, db)

    connect.assert_not_called()
    assert sizes.disk_bytes > 0


def test_list_tables_called_once(tmp_path: Path) -> None:
    _tables(tmp_path, "chunks", "pages")
    db = MagicMock(wraps=get_db(tmp_path))

    TableProbe.sizes(tmp_path, db)

    assert db.list_tables.call_count == 1
