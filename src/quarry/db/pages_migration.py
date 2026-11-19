"""One-time move of inline page text from the chunks table to the pages table."""

from __future__ import annotations

import enum
import hashlib
import logging
from typing import TYPE_CHECKING, Self, final

from quarry.db.adaptive_scan import AdaptiveScan
from quarry.db.page_store import PageStore
from quarry.db.row_budget import RowBudget, RowBudgetError
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
class _SeenPages:
    """A digest of the first inline text seen for each page.

    Holding a fixed-size digest, not the text, keeps memory bounded by the number
    of pages however large they are, and still tells a later, different value for
    the same page from a repeat of the first.
    """

    __slots__ = ("_first",)

    _first: dict[tuple[str, str, int], bytes]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._first = {}
        return self

    def is_new(self, page: PageRecord) -> bool:
        """Record *page* and return whether its key had not been seen."""
        key = (page.collection, page.document_name, page.page_number)
        if key in self._first:
            return False
        self._first[key] = self._digest(page.text)
        return True

    def conflicts(self, page: PageRecord) -> int:
        """Return 1 when *page* differs from the first text seen for its key."""
        key = (page.collection, page.document_name, page.page_number)
        return int(self._first[key] != self._digest(page.text))

    @staticmethod
    def _digest(text: str) -> bytes:
        return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).digest()


@final
class PagesMigration:
    """Backfill ``pages`` from ``chunks.page_raw_text``, verify, then drop the column.

    A chunk written after the pages table existed has no inline text (null), so a
    null is never page content: it is skipped, and the page its writer put is the
    one that counts.  The backfill reads in small adaptive batches, writes each
    batch once, and skips a page already stored with equal text, so a rerun after
    an interrupted or incomplete run does little work.  Two different inline
    values for one page, or a page over the budget, leave the column in place.
    The column is dropped only when a second pass finds every inline value equal
    to the stored page and every chunk's page present.
    """

    __slots__ = ("_budget", "_db", "_pages")

    _budget: RowBudget
    _db: LanceDB
    _pages: PageStore

    def __new__(cls, db: LanceDB) -> Self:
        """Prepare page backfill and verification over an open database."""
        self = super().__new__(cls)
        self._budget = RowBudget()
        self._db = db
        self._pages = PageStore(db)
        return self

    def run_failing_open(self) -> MigrationOutcome:
        """Run the migration; a failure is logged and reported as incomplete.

        For daemon startup, which must keep serving when a legacy table cannot be
        migrated: the inline column is only dropped after a clean verification, so
        a failure leaves every page readable.
        """
        try:
            return self.run()
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "pages migration failed; %s kept on the chunks table",
                LEGACY_COLUMN,
                exc_info=True,
            )
            return MigrationOutcome.INCOMPLETE

    def run(self) -> MigrationOutcome:
        """Migrate when the chunks table still holds page text; report the result."""
        if TABLE_NAME not in self._db.list_tables().tables:
            return MigrationOutcome.NOT_NEEDED
        table = self._db.open_table(TABLE_NAME)
        if LEGACY_COLUMN not in {f.name for f in table.schema}:
            return MigrationOutcome.NOT_NEEDED
        if self._backfill(table) or not self._verified(table):
            logger.warning(
                "pages migration incomplete; %s kept on the chunks table", LEGACY_COLUMN
            )
            return MigrationOutcome.INCOMPLETE
        table.drop_columns([LEGACY_COLUMN])
        logger.info("pages migration complete; dropped %s", LEGACY_COLUMN)
        return MigrationOutcome.MIGRATED

    def _backfill(self, table: LanceTable) -> int:
        """Write every distinct inline page; return how many could not be moved."""
        seen = _SeenPages()
        pending: list[PageRecord] = []
        unmoved = 0
        for batch in AdaptiveScan(table, _SCAN_COLUMNS, start=_START_BATCH_ROWS):
            for page in filter(None, map(self._page_of, batch)):
                if not seen.is_new(page):
                    unmoved += seen.conflicts(page)
                elif not self._stored_equal(page):
                    pending.append(page)
            if len(pending) >= _PUT_PAGES:
                unmoved += self._put(pending)
                pending = []
        return unmoved + self._put(pending)

    def _put(self, pages: list[PageRecord]) -> int:
        """Write *pages* in one commit; return how many were over the budget."""
        accepted = [p for p in pages if self._fits(p)]
        if accepted:
            self._pages.put(accepted)
        return len(pages) - len(accepted)

    def _fits(self, page: PageRecord) -> bool:
        try:
            self._budget.require_page(page.text)
        except RowBudgetError:
            logger.warning(
                "page %d of a document is over the page budget; not migrated",
                page.page_number,
            )
            return False
        return True

    def _stored_equal(self, page: PageRecord) -> bool:
        stored = self._pages.get_page_text(
            page.document_name, page.page_number, page.collection
        )
        return stored == page.text

    def _verified(self, table: LanceTable) -> bool:
        """Return whether every inline value equals its stored page.

        Each page is read from the pages table once, on its first chunk; later
        chunks of the page are compared against that first value by digest.
        """
        seen = _SeenPages()
        for batch in AdaptiveScan(table, _SCAN_COLUMNS, start=_START_BATCH_ROWS):
            for page in filter(None, map(self._page_of, batch)):
                if seen.is_new(page):
                    if not self._stored_equal(page):
                        return False
                elif seen.conflicts(page):
                    return False
        return True

    @staticmethod
    def _page_of(row: dict[str, object]) -> PageRecord | None:
        """Return the page *row* carries inline, or ``None`` when its text is null."""
        text = row[LEGACY_COLUMN]
        if text is None:
            return None
        return PageRecord(
            str(row["collection"]),
            str(row["document_name"]),
            int(str(row["page_number"])),
            str(text),
        )
