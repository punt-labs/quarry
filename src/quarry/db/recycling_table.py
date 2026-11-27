"""A ``LanceTable`` proxy that reports index rebuilds and writes to its owner."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator
    from datetime import timedelta

    import pyarrow as pa
    from lancedb.index import FTS, Bitmap, BTree

    from quarry.db.table_changes import TableChanges
    from quarry.types import LanceQuery, LanceTable


class TableOwner(Protocol):
    """What a table reports to: the connection that opened it."""

    @property
    def changes(self) -> TableChanges:
        """The record of writes made through the owner's tables."""
        ...

    def note_index_rebuild(self) -> None:
        """Count one ``replace=True`` index rebuild toward connection recycling."""
        ...


@final
class RecyclingTable:
    """A ``LanceTable`` proxy that reports index rebuilds and writes to its owner.

    Every method delegates to the wrapped table. ``create_fts_index`` and
    ``create_scalar_index`` — the ``replace=True`` rebuilds that leak
    descriptors — additionally notify the owning connection so it can recycle
    once enough have accumulated.  Every data write (rows added or deleted,
    columns added or dropped) is recorded on the owner's
    :class:`~quarry.db.table_changes.TableChanges` once it has finished, so the
    finalize can tell a table that changed from one that did not.  Compaction and
    index builds are maintenance, not writes, and are not recorded.
    """

    __slots__ = ("_owner", "_table")

    _table: LanceTable
    _owner: TableOwner

    def __new__(cls, table: LanceTable, owner: TableOwner) -> Self:
        """Wrap a table so index rebuilds and writes are reported to *owner*."""
        self = super().__new__(cls)
        self._table = table
        self._owner = owner
        return self

    @property
    def uri(self) -> str:
        """Filesystem URI of the underlying ``.lance`` table directory."""
        return self._table.uri

    @property
    def schema(self) -> pa.Schema:
        """Arrow schema of the wrapped table."""
        return self._table.schema

    def add(self, data: list[dict[str, object]]) -> None:
        """Append rows to the wrapped table."""
        with self._recorded_write():
            self._table.add(data)

    def search(
        self,
        query: list[float] | str | None = None,
        query_type: str | None = None,
    ) -> LanceQuery:
        """Start a query against the wrapped table."""
        return self._table.search(query, query_type)

    def count_rows(self, predicate: str | None = None) -> int:
        """Count rows matching *predicate* (all rows when ``None``)."""
        return self._table.count_rows(predicate)

    def delete(self, predicate: str) -> None:
        """Delete rows matching *predicate*."""
        with self._recorded_write():
            self._table.delete(predicate)

    def add_columns(self, transforms: dict[str, str]) -> None:
        """Add computed columns to the wrapped table."""
        with self._recorded_write():
            self._table.add_columns(transforms)

    def drop_columns(self, columns: list[str]) -> None:
        """Drop *columns* from the wrapped table (metadata-only)."""
        with self._recorded_write():
            self._table.drop_columns(columns)

    def stats(self) -> object:
        """Return the wrapped table's fragment statistics (loads no row data)."""
        return self._table.stats()

    def optimize(self, *, cleanup_older_than: timedelta | None = None) -> object:
        """Compact data fragments and prune old manifest versions."""
        return self._table.optimize(cleanup_older_than=cleanup_older_than)

    def create_index(
        self,
        column: str,
        *,
        config: BTree | Bitmap | FTS,
        replace: bool = False,
    ) -> None:
        """Build a scalar or FTS index; report only ``replace=True``, which leaks fds.

        Reports to the owning connection so it can recycle once enough
        ``replace=True`` rebuilds have accrued.
        """
        self._table.create_index(column, config=config, replace=replace)
        if replace:
            self._owner.note_index_rebuild()

    @contextmanager
    def _recorded_write(self) -> Generator[None]:
        """Record a write when it ends, even a failed one that may have applied."""
        try:
            yield
        finally:
            self._owner.changes.note_write()
