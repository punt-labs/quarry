"""Read-only probes of the database tables that load no row data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, final

import lancedb

from quarry.db.page_store import PAGES_TABLE
from quarry.db.schema import TABLE_NAME

if TYPE_CHECKING:
    from pathlib import Path


@final
@dataclass(frozen=True, slots=True)
class StorageSizes:
    """On-disk and live bytes of the chunks and pages tables; zero when absent.

    The two diverge when compaction fails: superseded files pile up on disk while
    the live data stays small, so their ratio is the bloat signal.
    """

    disk_bytes: int
    live_bytes: int

    @property
    def ratio(self) -> float:
        """On-disk bytes per live byte (a zero live size counts as one byte)."""
        return self.disk_bytes / max(self.live_bytes, 1)


@final
class TableProbe:
    """Open and measure the chunks and pages tables of the database at a path."""

    __slots__ = ()

    _TABLES: ClassVar[tuple[str, ...]] = (TABLE_NAME, PAGES_TABLE)

    @classmethod
    def sizes(cls, db_path: Path) -> StorageSizes:
        """Return the tables' on-disk bytes and their live-data estimate.

        Live bytes come from each table's own fragment statistics, so no row is
        loaded; disk bytes are the sizes of the files under each table directory.
        Page text lives in its own table and bloats the same way chunks do, so
        both count.
        """
        db = lancedb.connect(str(db_path))
        present = [t for t in cls._TABLES if t in db.list_tables().tables]
        return StorageSizes(
            disk_bytes=sum(cls._disk_bytes(db_path / f"{t}.lance") for t in present),
            live_bytes=sum(cls._live_bytes(db.open_table(t).stats()) for t in present),
        )

    @staticmethod
    def _disk_bytes(table_dir: Path) -> int:
        return sum(f.stat().st_size for f in table_dir.rglob("*") if f.is_file())

    @staticmethod
    def _live_bytes(stats: object) -> int:
        """Read ``total_bytes`` off the table statistics.

        lancedb annotates ``stats()`` as a ``TableStatistics`` object but returns
        a plain dict, so the shape is checked here rather than trusted.
        """
        total = stats.get("total_bytes") if isinstance(stats, dict) else None
        if not isinstance(total, int):
            msg = f"table statistics carry no total_bytes: {type(stats).__name__}"
            raise ValueError(msg)
        return total
