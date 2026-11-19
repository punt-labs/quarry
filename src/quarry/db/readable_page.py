"""The text of a page a reader may see: a live chunk must still refer to it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry._sql import escape_sql
from quarry.db.page_store import PageStore
from quarry.db.schema import TABLE_NAME

if TYPE_CHECKING:
    from quarry.types import LanceDB, LanceTable

LEGACY_COLUMN = "page_raw_text"


@final
class ReadablePage:
    """Serve a page's text only while a chunk of it exists.

    A page with no chunk is an orphan left by a crash or a delete, awaiting the
    sweep, and its text must not be served.  A page missing from the pages table
    is read from the inline column a migration has not yet dropped, so a page too
    large to move stays readable.
    """

    __slots__ = ("_db", "_pages")

    _db: LanceDB
    _pages: PageStore

    def __new__(cls, db: LanceDB) -> Self:
        """Pair page storage with chunk checks that control page visibility."""
        self = super().__new__(cls)
        self._db = db
        self._pages = PageStore(db)
        return self

    def text_of(
        self, document_name: str, page_number: int, collection: str | None
    ) -> str:
        """Return the page's text; raise ``KeyError`` when no live chunk holds it.

        With no *collection* given, the live chunk decides it, and that same
        collection selects the stored page, so a live chunk in one collection can
        never authorize another collection's page of the same name and number.
        """
        table = self._chunks()
        if table is None:
            raise KeyError(page_number)
        resolved = self._live_collection(
            table, self._predicate(document_name, page_number, collection)
        )
        stored = self._pages.get_page_text(document_name, page_number, resolved)
        if stored is not None:
            return stored
        inline = self._inline(
            table, self._predicate(document_name, page_number, resolved)
        )
        if inline is None:
            raise KeyError(page_number)
        return inline

    def _chunks(self) -> LanceTable | None:
        if TABLE_NAME not in self._db.list_tables().tables:
            return None
        return self._db.open_table(TABLE_NAME)

    @staticmethod
    def _live_collection(table: LanceTable, predicate: str) -> str:
        """Return the collection of the first chunk matching *predicate*."""
        rows = table.search().where(predicate).select(["collection"]).limit(1).to_list()
        if not rows:
            raise KeyError(predicate)
        return str(rows[0]["collection"])

    @staticmethod
    def _inline(table: LanceTable, predicate: str) -> str | None:
        if LEGACY_COLUMN not in {f.name for f in table.schema}:
            return None
        rows = (
            table.search()
            .where(f"{predicate} AND {LEGACY_COLUMN} IS NOT NULL")
            .select([LEGACY_COLUMN])
            .limit(1)
            .to_list()
        )
        return str(rows[0][LEGACY_COLUMN]) if rows else None

    @staticmethod
    def _predicate(document_name: str, page_number: int, collection: str | None) -> str:
        predicate = (
            f"document_name = '{escape_sql(document_name)}'"
            f" AND page_number = {page_number}"
        )
        if collection:
            predicate += f" AND collection = '{escape_sql(collection)}'"
        return predicate
