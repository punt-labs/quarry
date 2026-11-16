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
_CHUNK_QUERY_ROWS = 1


@final
class OrphanPageSweep:
    """Delete pages that have no chunk and are past the grace period.

    A writer puts a page before its chunks, so a young page without chunks may
    be mid-write; only an old one is an orphan left by a crash.  Old pages are
    read in bounded batches that advance past live pages as well as deleted
    ones, and each candidate is checked against the chunks table on its own, so
    no fixed result limit can hide an orphan or make a live page look orphaned.
    """

    __slots__ = ("_db", "_pages")

    _db: LanceDB
    _pages: PageStore

    def __new__(cls, db: LanceDB) -> Self:
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
            orphans = sum(self._sweep_one(row) for row in batch)
            swept += orphans
            offset += len(batch) - orphans
        if swept:
            logger.info("swept %d orphan pages", swept)
        return swept

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

    def _sweep_one(self, row: dict[str, object]) -> int:
        """Delete *row*'s page if no chunk holds it; return 1 when deleted."""
        predicate = (
            f"collection = '{escape_sql(str(row['collection']))}'"
            f" AND document_name = '{escape_sql(str(row['document_name']))}'"
            f" AND page_number = {int(str(row['page_number']))}"
        )
        with WRITE_LOCK:
            if self._has_chunk(predicate):
                return 0
            self._pages.delete_where(predicate)
        return 1

    def _has_chunk(self, predicate: str) -> bool:
        if TABLE_NAME not in self._db.list_tables().tables:
            return False
        chunks = self._db.open_table(TABLE_NAME)
        found = (
            chunks.search()
            .where(predicate)
            .select(["page_number"])
            .limit(_CHUNK_QUERY_ROWS)
            .to_list()
        )
        return bool(found)
