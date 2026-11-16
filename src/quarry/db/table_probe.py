"""Read-only probes of the database tables that load no row data."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, final

from quarry.db.page_store import PAGES_TABLE
from quarry.db.schema import TABLE_NAME
from quarry.db.storage import get_db

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.types import LanceDB


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
    def sizes(cls, db_path: Path, db: LanceDB | None = None) -> StorageSizes:
        """Return the tables' on-disk bytes and their live-data estimate.

        Live bytes come from each table's own fragment statistics, so no row is
        loaded; disk bytes are the sizes of the files under each table directory.
        Page text lives in its own table and bloats the same way chunks do, so
        both count.

        ``db`` is the caller's managed connection.  The daemon passes its own so
        a status call opens no descriptors of its own; only the short-lived
        ``quarry doctor`` process, which has no daemon connection, omits it.
        """
        conn = db if db is not None else get_db(db_path)
        names = conn.list_tables().tables
        present = [t for t in cls._TABLES if t in names]
        return StorageSizes(
            disk_bytes=sum(cls._disk_bytes(db_path / f"{t}.lance") for t in present),
            live_bytes=sum(
                cls._live_bytes(conn.open_table(t).stats()) for t in present
            ),
        )

    @staticmethod
    def _disk_bytes(table_dir: Path) -> int:
        """Sum the regular files under *table_dir*, skipping any that vanish.

        Compaction and cleanup delete files while this walks, and a bloated,
        churning table is exactly when the walk runs; a file that disappears
        between listing and stat has simply left the footprint.  Any other
        ``OSError`` still propagates so an unmeasurable table is reported as such.
        """
        total = 0
        for path in table_dir.rglob("*"):
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
        return total

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
