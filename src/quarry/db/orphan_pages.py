"""Sweep pages no chunk refers to, once they are older than the grace period."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self, final

from quarry._sql import escape_sql
from quarry.db.page_store import WRITE_LOCK, PageStore
from quarry.db.schema import TABLE_NAME

if TYPE_CHECKING:
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)

GRACE_PERIOD = timedelta(hours=24)
_BATCH_ROWS = 1_000
_CHUNK_CAP = 64_000


@final
class OrphanPageSweep:
    """Delete pages that have no chunk and are past the grace period.

    A writer puts a page before its chunks, so a young page without chunks may
    be mid-write; only an old one is an orphan left by a crash.  Old pages are
    read in bounded batches that advance past live pages as well as deleted
    ones.  Liveness is looked up for a whole batch at once, outside the write
    lock, and every page about to be deleted is rechecked under the lock, so a
    long sweep never stalls ingest and a chunk written meanwhile keeps its page.
    """

    __slots__ = ("_db", "_pages")

    _db: LanceDB
    _pages: PageStore

    def __new__(cls, db: LanceDB) -> Self:
        """Share a database connection with the orphaned-page store."""
        self = super().__new__(cls)
        self._db = db
        self._pages = PageStore(db)
        return self

    def run(self, now: datetime | None = None) -> int:
        """Delete the orphan pages older than the grace period; return the count."""
        if self._pages.open_or_none() is None:
            return 0
        cutoff = (now or datetime.now(UTC)) - GRACE_PERIOD
        stamp = cutoff.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        old = f"ingestion_timestamp < timestamp '{stamp}'"
        swept, offset = 0, 0
        while batch := self._batch(old, offset):
            deleted, kept = self._sweep_batch(batch)
            swept += deleted
            offset += kept
        if swept:
            logger.info("swept %d orphan pages", swept)
        return swept

    def _sweep_batch(self, batch: list[dict[str, object]]) -> tuple[int, int]:
        """Delete *batch*'s orphans; return ``(deleted, rows to skip next time)``."""
        live = self._batch_live_pages(batch)
        orphans = [row for row in batch if self._key(row) not in live]
        deleted = sum(self._delete_if_still_orphan(row) for row in orphans)
        return deleted, len(batch) - deleted

    def _batch(self, old: str, offset: int) -> list[dict[str, object]]:
        # A handle opened before a delete keeps reading the old version, so each
        # batch reopens the table.
        table = self._pages.open_or_none()
        if table is None:
            return []
        query = (
            table.search()
            .where(old)
            .select(["collection", "document_name", "page_number"])
            .limit(_BATCH_ROWS)
        )
        return query.offset(offset).to_list()

    def _batch_live_pages(
        self, rows: list[dict[str, object]]
    ) -> set[tuple[str, str, int]]:
        """Return the pages among *rows* that some chunk holds, in one query.

        Runs without the write lock: a chunk that appears after this read is
        caught by the recheck made under the lock just before a delete.
        """
        found = self._chunks_of(rows)
        live = {self._key(row) for row in found}
        if len(found) < _CHUNK_CAP:
            return live
        # The result may be cut short, so a page missing from it is not proven
        # orphaned; check those pages one by one.
        return live | {
            self._key(row) for row in rows if self._has_chunk(self._predicate(row))
        }

    def _chunks_of(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        """Return up to ``_CHUNK_CAP`` chunk rows that hold any page in *rows*."""
        if TABLE_NAME not in self._db.list_tables().tables:
            return []
        wanted = " OR ".join(f"({self._predicate(row)})" for row in rows)
        return (
            self._db.open_table(TABLE_NAME)
            .search()
            .where(wanted)
            .select(["collection", "document_name", "page_number"])
            .limit(_CHUNK_CAP)
            .to_list()
        )

    def _delete_if_still_orphan(self, row: dict[str, object]) -> int:
        """Delete *row*'s page unless a chunk now holds it; return 1 when deleted."""
        predicate = self._predicate(row)
        with WRITE_LOCK:
            if self._has_chunk(predicate):
                return 0
            self._pages.delete_where(predicate)
        return 1

    def _has_chunk(self, predicate: str) -> bool:
        if TABLE_NAME not in self._db.list_tables().tables:
            return False
        found = (
            self._db.open_table(TABLE_NAME)
            .search()
            .where(predicate)
            .select(["page_number"])
            .limit(1)
            .to_list()
        )
        return bool(found)

    @staticmethod
    def _key(row: dict[str, object]) -> tuple[str, str, int]:
        return (
            str(row["collection"]),
            str(row["document_name"]),
            int(str(row["page_number"])),
        )

    @staticmethod
    def _predicate(row: dict[str, object]) -> str:
        return (
            f"collection = '{escape_sql(str(row['collection']))}'"
            f" AND document_name = '{escape_sql(str(row['document_name']))}'"
            f" AND page_number = {int(str(row['page_number']))}"
        )
