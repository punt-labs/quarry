"""One-time move of inline page text from the chunks table to the pages table."""

from __future__ import annotations

import enum
import logging
from typing import TYPE_CHECKING, Self, final

from quarry.db.adaptive_scan import AdaptiveScan
from quarry.db.page_store import PageStore
from quarry.db.row_budget import RowBudgetError
from quarry.db.schema import TABLE_NAME
from quarry.page_record import PageRecord

if TYPE_CHECKING:
    from quarry.types import LanceDB, LanceTable

logger = logging.getLogger(__name__)

LEGACY_COLUMN = "page_raw_text"
_SCAN_COLUMNS = ["collection", "document_name", "page_number", LEGACY_COLUMN]
_START_BATCH_ROWS = 256
_PUT_PAGES = 64


class MigrationOutcome(enum.Enum):
    """What a migration run did."""

    NOT_NEEDED = "not needed"
    MIGRATED = "migrated"
    INCOMPLETE = "incomplete"


@final
class PagesMigration:
    """Backfill ``pages`` from ``chunks.page_raw_text``, verify, then drop the column.

    The backfill reads in small adaptive batches (a poisoned table overflows a
    large read) and upserts, so an interrupted run is finished by the next one.
    The column is dropped only after a second pass proves every page is in the
    pages table with equal text; any gap leaves the column, and the data, in place.
    """

    __slots__ = ("_db", "_pages")

    _db: LanceDB
    _pages: PageStore

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        self._pages = PageStore(db)
        return self

    def run(self) -> MigrationOutcome:
        """Migrate when the chunks table still holds page text; report the result."""
        if TABLE_NAME not in self._db.list_tables().tables:
            return MigrationOutcome.NOT_NEEDED
        table = self._db.open_table(TABLE_NAME)
        if LEGACY_COLUMN not in {f.name for f in table.schema}:
            return MigrationOutcome.NOT_NEEDED
        skipped = self._backfill(table)
        if skipped or not self._verified(table):
            logger.warning(
                "pages migration incomplete; %s kept on the chunks table", LEGACY_COLUMN
            )
            return MigrationOutcome.INCOMPLETE
        table.drop_columns([LEGACY_COLUMN])
        logger.info("pages migration complete; dropped %s", LEGACY_COLUMN)
        return MigrationOutcome.MIGRATED

    def _backfill(self, table: LanceTable) -> int:
        """Upsert every distinct page; return how many were refused."""
        seen: set[tuple[str, str, int]] = set()
        pending: list[PageRecord] = []
        skipped = 0
        for batch in AdaptiveScan(table, _SCAN_COLUMNS, start=_START_BATCH_ROWS):
            for row in batch:
                page = self._page_of(row)
                key = (page.collection, page.document_name, page.page_number)
                if key in seen:
                    continue
                seen.add(key)
                pending.append(page)
                if len(pending) >= _PUT_PAGES:
                    skipped += self._put(pending)
                    pending = []
        return skipped + self._put(pending)

    def _put(self, pages: list[PageRecord]) -> int:
        """Write *pages*; return how many were refused for exceeding the budget."""
        refused = 0
        for page in pages:
            try:
                self._pages.put([page])
            except RowBudgetError:
                refused += 1
                logger.warning(
                    "page %d of a document is over the page budget; not migrated",
                    page.page_number,
                )
        return refused

    def _verified(self, table: LanceTable) -> bool:
        """Return whether every chunk's page is stored with equal text."""
        checked: set[tuple[str, str, int]] = set()
        for batch in AdaptiveScan(table, _SCAN_COLUMNS, start=_START_BATCH_ROWS):
            for row in batch:
                page = self._page_of(row)
                key = (page.collection, page.document_name, page.page_number)
                if key in checked:
                    continue
                checked.add(key)
                stored = self._pages.get_page_text(
                    page.document_name, page.page_number, page.collection
                )
                if stored != page.text:
                    return False
        return True

    @staticmethod
    def _page_of(row: dict[str, object]) -> PageRecord:
        return PageRecord(
            str(row["collection"]),
            str(row["document_name"]),
            int(str(row["page_number"])),
            str(row[LEGACY_COLUMN]),
        )
