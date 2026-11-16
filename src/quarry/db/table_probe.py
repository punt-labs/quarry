"""Read-only probes of the chunks table that load no row data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

import lancedb

from quarry.db.schema import TABLE_NAME

if TYPE_CHECKING:
    from pathlib import Path


@final
@dataclass(frozen=True, slots=True)
class StorageSizes:
    """On-disk and live bytes of the chunks table; both zero when it is absent.

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
    """Open and measure the chunks table of the database at a path."""

    __slots__ = ()

    @classmethod
    def sizes(cls, db_path: Path) -> StorageSizes:
        """Return the table's on-disk bytes and its live-data estimate.

        Live bytes come from the table's own fragment statistics, so no row is
        loaded; disk bytes are the sizes of the files under the table directory.
        """
        db = lancedb.connect(str(db_path))
        if TABLE_NAME not in db.list_tables().tables:
            return StorageSizes(disk_bytes=0, live_bytes=0)
        table_dir = db_path / f"{TABLE_NAME}.lance"
        return StorageSizes(
            disk_bytes=sum(
                f.stat().st_size for f in table_dir.rglob("*") if f.is_file()
            ),
            live_bytes=cls._live_bytes(db.open_table(TABLE_NAME).stats()),
        )

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
