"""List the stored document names that start with a prefix, bounded by it."""

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
    """Query the document names in a collection that begin with a given prefix.

    One row is read per document, not one per chunk: the ingest funnel numbers a
    document's chunks as one contiguous sequence from zero, so every stored
    document has a chunk 0.  The match ignores case; the caller decides what a
    case difference means.
    """

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    def names(self, prefix: str, collection: str) -> list[str]:
        """Return the distinct names in *collection* starting with *prefix*.

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
