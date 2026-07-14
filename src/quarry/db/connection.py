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
rebuilds, swaps the underlying connection. Recycling is attempted at any
connection boundary — ``open_table``, ``list_tables``, or ``create_table`` —
which only avoids swapping mid-operation; the wrapper does *not* track live
table handles or wait on them. Swapping ``_inner`` supersedes the old
connection, but any ``RecyclingTable`` handles already handed out keep working
against it; the old connection's cached descriptors are released only once all
those handles are dropped *and* cyclic GC collects the reference cycle the
lancedb binding holds it in — not immediately at the swap. The wrapper is a
transparent drop-in for the ``LanceDB`` protocol: callers neither know nor care
that the connection underneath them is periodically replaced.
"""

from __future__ import annotations

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
        """Build a scalar index; report only ``replace=True``, which leaks fds."""
        self._table.create_scalar_index(column, index_type=index_type, replace=replace)
        if replace:
            self._conn.note_index_rebuild()

    def create_fts_index(self, column: str, *, replace: bool = False) -> None:
        """Build the full-text index; report only ``replace=True``, which leaks fds."""
        self._table.create_fts_index(column, replace=replace)
        if replace:
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
        "_recycling",
    )

    _connect: Callable[[], LanceDB]
    _inner: LanceDB
    _rebuilds: int
    _recycle_after: int
    _recycle: bool
    _recycling: bool
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
        self._recycling = False
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
        """Recycle if due, then create *name* as a rebuild-reporting table."""
        self._maybe_recycle()
        table = self._inner.create_table(name, data=data, schema=schema)
        return RecyclingTable(table, self)

    def note_index_rebuild(self) -> None:
        """Record one ``replace=True`` index rebuild; arm recycling at the cap.

        Recycling is deferred to the next connection boundary
        (:meth:`open_table`, :meth:`list_tables`, or :meth:`create_table`) so it
        never swaps mid-operation. The deferral does not gate on live table
        handles: handles already handed out keep working against the old
        connection after the swap.
        """
        with self._lock:
            self._rebuilds += 1
            if self._rebuilds >= self._recycle_after:
                self._recycle = True

    def _maybe_recycle(self) -> None:
        """Reopen the underlying connection when a recycle is armed.

        The reopen is *retry-safe*. A failing ``_connect()`` — most likely
        exactly when descriptors are exhausted and a fresh open hits ``EMFILE``
        — must not lose the pending recycle. So the armed ``_recycle`` flag and
        the rebuild counter are cleared only *after* a successful reopen: if
        ``_connect()`` raises, ``_recycle`` stays armed, the old ``_inner``
        stays intact and usable, and the exception propagates. The next
        connection boundary (``open_table``, ``list_tables``, or
        ``create_table``) then retries the reopen. A transient ``_recycling``
        guard serialises the reopen so a concurrent boundary serves the
        still-valid old connection rather than double-opening a second one.

        Reassigning ``_inner`` drops the *wrapper's* reference to the old
        connection; once any still-live table handles referencing it are also
        dropped, it is unreferenced except for the reference cycle the lancedb
        binding holds it in. Plain refcounting does not free that cycle — only
        cyclic GC reclaims it and releases its cached index-file descriptors
        (proven: the fd ceiling over 200 optimize cycles is ~53 with an explicit
        ``gc.collect()`` here and ~107 without, so refcounting alone is not doing
        the freeing).

        No explicit ``gc.collect()`` is forced here: on the daemon, the recycle
        fires during ``sync``'s optimize, and ``SyncFinalizer`` already runs
        ``gc.collect(2)`` at the end of every sync (sync_finalize.py), so the
        superseded connection is reclaimed on that pass. Elsewhere Python's
        automatic gen-2 collection bounds it. A per-recycle full-heap collection
        would be redundant work on the hot sync path.
        """
        with self._lock:
            if not self._recycle or self._recycling:
                return
            self._recycling = True
        succeeded = False
        try:
            fresh = self._connect()
            # Swap and disarm atomically under one lock: a concurrent boundary
            # must never observe the recycle still armed with the guard already
            # dropped, or it would open a second connection on top of this one.
            with self._lock:
                self._inner = fresh
                self._recycle = False
                self._rebuilds = 0
                self._recycling = False
            succeeded = True
        finally:
            if not succeeded:
                # The reopen raised: drop only the in-progress guard, leaving
                # _recycle armed so the next boundary retries onto the still-live
                # old connection. The exception propagates to the caller.
                with self._lock:
                    self._recycling = False
        logger.debug("Recycled LanceDB connection to release cached fds")
