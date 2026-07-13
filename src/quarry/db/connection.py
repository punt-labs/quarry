"""A self-recycling LanceDB connection that bounds file-descriptor growth.

LanceDB's Rust core caches index-file readers on a connection and never evicts
them when ``create_fts_index(replace=True)`` (or ``create_scalar_index``)
supersedes an index generation. ``replace=True`` writes a new ``_indices/<uuid>/``
generation and deletes the previous one from disk, but the cached readers to the
now-deleted files stay open. A short-lived CLI process never notices — it does one
optimize and exits. A long-lived daemon rebuilds the index on every sync, so the
descriptors accumulate one generation at a time until the process hits its
``RLIMIT_NOFILE`` and every ``open()`` fails with ``EMFILE`` (surfacing as HTTP 500
on ``quarry find``).

``LanceConnection`` wraps a raw connection and, after a bounded number of index
rebuilds, drops and reopens the underlying connection. Dropping the last reference
to the old connection releases the Rust session cache and, with it, the leaked
descriptors. Recycling happens only at an ``open_table``/``list_tables`` boundary —
never while a table handle from the old connection is live — so the release is
clean rather than partial. The wrapper is a transparent drop-in for the ``LanceDB``
protocol: callers neither know nor care that the connection underneath them is
periodically replaced.
"""

from __future__ import annotations

import gc
import logging
import threading
from typing import TYPE_CHECKING, Self

from quarry.types import LanceDB, LanceTable

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import timedelta

    import pyarrow as pa

    from quarry.types import LanceQuery, ListTablesResult

logger = logging.getLogger(__name__)

# Recycle the connection after this many index rebuilds. Each rebuild leaks a
# small, fixed number of descriptors, so the ceiling is roughly
# ``recycle_after * fds_per_rebuild`` above the process baseline — comfortably
# under a 256 soft limit for the daemon's two connections.
_DEFAULT_RECYCLE_AFTER = 20


class RecyclingTable:
    """A ``LanceTable`` proxy that reports index rebuilds to its connection.

    Every method delegates to the wrapped table. ``create_fts_index`` and
    ``create_scalar_index`` — the ``replace=True`` rebuilds that leak
    descriptors — additionally notify the owning :class:`LanceConnection` so it
    can recycle once enough have accumulated.
    """

    __slots__ = ("_conn", "_table")

    _table: LanceTable
    _conn: LanceConnection

    def __new__(cls, table: LanceTable, conn: LanceConnection) -> Self:
        self = super().__new__(cls)
        self._table = table
        self._conn = conn
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
        self._table.delete(predicate)

    def add_columns(self, transforms: dict[str, str]) -> None:
        """Add computed columns to the wrapped table."""
        self._table.add_columns(transforms)

    def optimize(self, *, cleanup_older_than: timedelta | None = None) -> object:
        """Compact data fragments and prune old manifest versions."""
        return self._table.optimize(cleanup_older_than=cleanup_older_than)

    def create_scalar_index(
        self,
        column: str,
        *,
        index_type: str = "BTREE",
        replace: bool = False,
    ) -> None:
        """Build a scalar index, then report the rebuild to the connection."""
        self._table.create_scalar_index(column, index_type=index_type, replace=replace)
        self._conn.note_index_rebuild()

    def create_fts_index(self, column: str, *, replace: bool = False) -> None:
        """Build the full-text index, then report the rebuild to the connection."""
        self._table.create_fts_index(column, replace=replace)
        self._conn.note_index_rebuild()


class LanceConnection:
    """A ``LanceDB`` connection that recycles itself to bound descriptor growth.

    Opens tables through :class:`RecyclingTable` so index rebuilds are counted.
    Once ``recycle_after`` rebuilds have accrued, the next table/list access
    reopens the underlying connection, releasing the descriptors the Rust core
    cached against superseded-and-deleted index generations.

    The physical connection is obtained through an injected ``connect`` factory
    rather than a path, so both the initial open and every recycle go through the
    caller's own opener — preserving the ``quarry.db.facade.get_db`` mock seam and
    keeping this class free of storage-layer imports.
    """

    __slots__ = (
        "_connect",
        "_inner",
        "_lock",
        "_rebuilds",
        "_recycle",
        "_recycle_after",
    )

    _connect: Callable[[], LanceDB]
    _inner: LanceDB
    _rebuilds: int
    _recycle_after: int
    _recycle: bool
    _lock: threading.Lock

    def __new__(
        cls,
        connect: Callable[[], LanceDB],
        *,
        recycle_after: int = _DEFAULT_RECYCLE_AFTER,
    ) -> Self:
        self = super().__new__(cls)
        self._connect = connect
        self._inner = connect()
        self._rebuilds = 0
        self._recycle_after = recycle_after
        self._recycle = False
        self._lock = threading.Lock()
        return self

    def list_tables(self) -> ListTablesResult:
        """Recycle if due, then list the underlying connection's tables."""
        self._maybe_recycle()
        return self._inner.list_tables()

    def open_table(self, name: str) -> LanceTable:
        """Recycle if due, then open *name* as a rebuild-reporting table."""
        self._maybe_recycle()
        return RecyclingTable(self._inner.open_table(name), self)

    def create_table(
        self,
        name: str,
        *,
        data: list[dict[str, object]],
        schema: object,
    ) -> LanceTable:
        """Create *name* and return it as a rebuild-reporting table."""
        table = self._inner.create_table(name, data=data, schema=schema)
        return RecyclingTable(table, self)

    def note_index_rebuild(self) -> None:
        """Record one ``replace=True`` index rebuild; arm recycling at the cap.

        Recycling is deferred to the next :meth:`open_table`/:meth:`list_tables`
        boundary so it never fires while a table handle from the current
        connection is still live.
        """
        with self._lock:
            self._rebuilds += 1
            if self._rebuilds >= self._recycle_after:
                self._recycle = True

    def _maybe_recycle(self) -> None:
        """Reopen the underlying connection when a recycle is armed.

        Reassigning ``_inner`` drops the last strong reference to the old
        connection; the collection releases its cached index-file descriptors.
        A targeted ``gc.collect()`` makes the release deterministic even if the
        Rust binding participates in a reference cycle.
        """
        with self._lock:
            if not self._recycle:
                return
            self._recycle = False
            self._rebuilds = 0
        self._inner = self._connect()
        gc.collect()
        logger.debug("Recycled LanceDB connection to release cached fds")
