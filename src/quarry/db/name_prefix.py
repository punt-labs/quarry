"""List the stored URL-sourced document names that start with a prefix."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry._sql import escape_sql
from quarry.db.schema import TABLE_NAME

if TYPE_CHECKING:
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)

_SCAN_LIMIT = 1_000_000


@final
class NamePrefix:
    """Query the URL-sourced document names in a collection under a given prefix.

    A document is URL-sourced when it was fetched from a URL, so its
    ``document_path`` is that URL; a document someone named with a URL
    (``remember``, ``learn``) has no such path and is a label, not a page.  One row
    is read per document, not one per chunk: the ingest funnel numbers a
    document's chunks as one contiguous sequence from zero, so every stored
    document has a chunk 0.  The match ignores case, in the name and in the
    path's scheme; the caller decides what a case difference means.
    """

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    def names(self, prefix: str, collection: str) -> list[str]:
        """Return the distinct URL-sourced names in *collection* under *prefix*.

        The SQL ``LIKE`` has its wildcards in *prefix* escaped, so it matches the
        prefix literally; the exact test is made here as well, because SQL and
        Python lower-case non-ASCII text differently.  A scan that reaches its
        row limit logs a warning: the list is then incomplete.
        """
        if TABLE_NAME not in self._db.list_tables().tables:
            return []
        predicate = (
            f"lower(document_name) LIKE '{self._pattern(prefix.lower())}%' ESCAPE '\\'"
            f" AND collection = '{escape_sql(collection)}'"
            " AND chunk_index = 0"
            " AND (lower(document_path) LIKE 'http://%'"
            " OR lower(document_path) LIKE 'https://%')"
        )
        query = self._db.open_table(TABLE_NAME).search().where(predicate)
        rows = query.limit(_SCAN_LIMIT).select(["document_name"]).to_list()
        if len(rows) >= _SCAN_LIMIT:
            logger.warning(
                "name prefix scan reached its %d-document limit; "
                "names beyond it are not listed",
                _SCAN_LIMIT,
            )
        wanted = prefix.lower()
        found = (str(row["document_name"]) for row in rows)
        return sorted({name for name in found if name.lower().startswith(wanted)})

    @staticmethod
    def _pattern(prefix: str) -> str:
        """Return *prefix* as a literal ``LIKE`` pattern for a SQL string."""
        literal = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return escape_sql(literal)
