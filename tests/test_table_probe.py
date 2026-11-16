"""``TableProbe`` measures the whole database, chunks and pages, without row data."""

from __future__ import annotations

from typing import TYPE_CHECKING

import lancedb

from quarry.db.table_probe import TableProbe

if TYPE_CHECKING:
    from pathlib import Path

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
