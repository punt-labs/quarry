"""The pages table: each page's full text, stored once per page."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

import pyarrow as pa

from quarry._sql import escape_sql
from quarry.db.document_ref import CollectionRef
from quarry.db.row_budget import RowBudget

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from quarry.db.document_ref import DocumentRef
    from quarry.page_record import PageRecord
    from quarry.types import LanceDB, LanceTable

logger = logging.getLogger(__name__)

PAGES_TABLE = "pages"
_FULL_SCAN_LIMIT = 1_000_000

# Serializes every write to the pages and chunks tables.  It is reentrant so a
# writer can hold it across a whole page-and-chunk operation while the page
# store takes it again for each page write.
WRITE_LOCK = threading.RLock()


@final
class PageStore:
    """Insert, read and delete page text in the pages table.

    A page is keyed by ``(collection, document_name, page_number)`` and written
    with an upsert, so re-putting a page replaces it and never duplicates it.
    Writers put a document's pages before its chunks and delete them after, so
    a chunk never lacks its page; a crash can only leave orphan pages.
    """

    __slots__ = ("_budget", "_db")

    _db: LanceDB
    _budget: RowBudget

    def __new__(cls, db: LanceDB) -> Self:
        """Bind page writes and reads to the default row-size budget."""
        self = super().__new__(cls)
        self._db = db
        self._budget = RowBudget()
        return self

    @staticmethod
    def schema() -> pa.Schema:
        """Return the PyArrow schema of the pages table."""
        return pa.schema(
            [
                pa.field("collection", pa.utf8()),
                pa.field("document_name", pa.utf8()),
                pa.field("page_number", pa.int32()),
                pa.field("text", pa.utf8()),
                pa.field("ingestion_timestamp", pa.timestamp("us", tz="UTC")),
            ]
        )

    def put(self, pages: Sequence[PageRecord]) -> int:
        """Upsert *pages*; return how many distinct pages were written.

        Raises :class:`RowBudgetError` before writing anything if a page is over
        the page budget.
        """
        distinct = {(p.collection, p.document_name, p.page_number): p for p in pages}
        if not distinct:
            return 0
        now = datetime.now(UTC)
        rows: list[dict[str, object]] = []
        for page in distinct.values():
            self._budget.require_page(page.text)
            rows.append(
                {
                    "collection": page.collection,
                    "document_name": page.document_name,
                    "page_number": page.page_number,
                    "text": page.text,
                    "ingestion_timestamp": now,
                }
            )
        with WRITE_LOCK:
            table = self._open_or_create(rows)
            if table is not None:
                table.delete(self._keys_predicate(distinct))
                table.add(rows)
        return len(rows)

    def get_page_text(
        self, document_name: str, page_number: int, collection: str | None = None
    ) -> str | None:
        """Return the stored text of a page, or ``None`` when it is absent."""
        table = self.open_or_none()
        if table is None:
            return None
        predicate = (
            f"document_name = '{escape_sql(document_name)}'"
            f" AND page_number = {page_number}"
        )
        if collection:
            predicate += f" AND {CollectionRef(collection).clause()}"
        rows = table.search().where(predicate).limit(1).select(["text"]).to_list()
        return str(rows[0]["text"]) if rows else None

    def count(self) -> int:
        """Return the number of stored pages."""
        table = self.open_or_none()
        return 0 if table is None else table.count_rows()

    def delete(self, ref: DocumentRef) -> None:
        """Delete every page of the document *ref* names."""
        self.delete_where(ref.predicate())

    def delete_collection(self, collection: str) -> None:
        """Delete every page of *collection*."""
        self.delete_where(CollectionRef(collection).clause())

    def delete_where(self, predicate: str) -> None:
        """Delete the pages *predicate* selects, serialized with every page write."""
        table = self.open_or_none()
        if table is None:
            return
        with WRITE_LOCK:
            table.delete(predicate)

    def open_or_none(self) -> LanceTable | None:
        """Return the pages table, or ``None`` when it does not exist."""
        if PAGES_TABLE not in self._db.list_tables().tables:
            return None
        try:
            return self._db.open_table(PAGES_TABLE)
        except ValueError as exc:
            if "not found" not in str(exc).lower():
                raise
            return None

    @staticmethod
    def _keys_predicate(pages: Mapping[tuple[str, str, int], PageRecord]) -> str:
        """Return the clause selecting exactly the stored rows *pages* replace."""
        return " OR ".join(
            f"(collection = '{escape_sql(c)}' AND document_name = '{escape_sql(d)}'"
            f" AND page_number = {n})"
            for c, d, n in pages
        )

    def _open_or_create(self, rows: list[dict[str, object]]) -> LanceTable | None:
        """Return the table to upsert into, or ``None`` when creation wrote *rows*."""
        table = self.open_or_none()
        if table is not None:
            return table
        try:
            self._db.create_table(PAGES_TABLE, data=rows, schema=self.schema())
        except ValueError as exc:
            if "already exists" not in str(exc).lower():
                raise
            return self._db.open_table(PAGES_TABLE)
        logger.debug("created %s table", PAGES_TABLE)
        return None
