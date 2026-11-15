"""Sweep pages no chunk refers to, once they are older than the grace period."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self, final

from quarry._sql import escape_sql
from quarry.db.page_store import PageStore
from quarry.db.schema import TABLE_NAME

if TYPE_CHECKING:
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)

GRACE_PERIOD = timedelta(hours=24)
_FULL_SCAN_LIMIT = 1_000_000


@final
class OrphanPageSweep:
    """Delete pages that have no chunk and are past the grace period.

    A writer puts a page before its chunks, so a young page without chunks may
    be mid-write; only an old one is an orphan left by a crash.
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
        table = self._pages.open_or_none()
        if table is None:
            return 0
        cutoff = (now or datetime.now(UTC)) - GRACE_PERIOD
        stamp = cutoff.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        old = (
            table.search()
            .where(f"ingestion_timestamp < timestamp '{stamp}'")
            .select(["collection", "document_name", "page_number"])
            .limit(_FULL_SCAN_LIMIT)
            .to_list()
        )
        by_document: dict[tuple[str, str], list[int]] = defaultdict(list)
        for row in old:
            key = (str(row["collection"]), str(row["document_name"]))
            by_document[key].append(int(str(row["page_number"])))
        swept = 0
        for (collection, name), numbers in by_document.items():
            held = self._pages_with_chunks(collection, name, numbers)
            orphans = [n for n in numbers if n not in held]
            if orphans:
                table.delete(self._predicate(collection, name, orphans))
                swept += len(orphans)
        if swept:
            logger.info("swept %d orphan pages", swept)
        return swept

    def _pages_with_chunks(
        self, collection: str, name: str, numbers: list[int]
    ) -> set[int]:
        if TABLE_NAME not in self._db.list_tables().tables:
            return set()
        chunks = self._db.open_table(TABLE_NAME)
        rows = (
            chunks.search()
            .where(self._predicate(collection, name, numbers))
            .select(["page_number"])
            .limit(_FULL_SCAN_LIMIT)
            .to_list()
        )
        return {int(str(row["page_number"])) for row in rows}

    @staticmethod
    def _predicate(collection: str, name: str, numbers: list[int]) -> str:
        listed = ", ".join(str(n) for n in numbers)
        return (
            f"collection = '{escape_sql(collection)}'"
            f" AND document_name = '{escape_sql(name)}'"
            f" AND page_number IN ({listed})"
        )
