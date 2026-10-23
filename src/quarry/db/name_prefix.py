"""List the stored document names that start with a prefix, bounded by it."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry._sql import escape_sql
from quarry.db.schema import TABLE_NAME

if TYPE_CHECKING:
    from quarry.types import LanceDB

_FULL_SCAN_LIMIT = 1_000_000


@final
class NamePrefix:
    """Query the document names in a collection that begin with a given prefix."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    def names(self, prefix: str, collection: str) -> list[str]:
        """Return the distinct names in *collection* starting with *prefix*.

        The SQL ``LIKE`` is a superset (its wildcards in *prefix* are not
        escaped); the exact prefix test is made here.
        """
        if TABLE_NAME not in self._db.list_tables().tables:
            return []
        predicate = (
            f"document_name LIKE '{escape_sql(prefix)}%'"
            f" AND collection = '{escape_sql(collection)}'"
        )
        query = self._db.open_table(TABLE_NAME).search().where(predicate)
        rows = query.limit(_FULL_SCAN_LIMIT).select(["document_name"]).to_list()
        found = (str(row["document_name"]) for row in rows)
        return sorted({name for name in found if name.startswith(prefix)})
