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
from typing import TYPE_CHECKING, Self

from quarry.db.reconnector import Reconnector
from quarry.db.recycle_gate import RecycleGate
from quarry.db.recycling_table import RecyclingTable
from quarry.db.table_changes import TableChanges

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from pathlib import Path

    from quarry.types import LanceDB, LanceTable, ListTablesResult

logger = logging.getLogger(__name__)

# Recycle the connection after this many index rebuilds. Each rebuild leaks a
# small, fixed number of descriptors that cyclic GC reclaims once the superseded
# connection is dropped, so one connection's descriptor use plateaus at roughly
# ``recycle_after * fds_per_rebuild`` above the process baseline.
#
# Post-DES-045 the daemon holds one such connection per roster database
# (watch_roster.py), not the two this bound was first sized for. The aggregate
# ceiling is therefore ``(N+1) * recycle_after * fds_per_rebuild + baseline`` for
# N sibling databases plus the active one — a constant for a fixed roster (each
# connection recycles on a bounded cadence, so the sum of per-connection plateaus
# is itself a plateau), not an unbounded climb. That aggregate is what a
# ~21-collection roster pushes above launchd's 256 soft limit, so DES-046 raises
# RLIMIT_NOFILE to 8192 at daemon start: the ceiling must fit under the raised
# limit, not the inherited 256. The recycler bounds each connection's growth;
# the process limit must also accommodate the aggregate roster.
_DEFAULT_RECYCLE_AFTER = 20


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

    __slots__ = ("_changes", "_gate", "_inner", "_reconnector")

    _changes: TableChanges
    _gate: RecycleGate
    _inner: LanceDB
    _reconnector: Reconnector

    def __new__(
        cls,
        connect: Callable[[], LanceDB],
        changes: TableChanges,
        *,
        recycle_after: int = _DEFAULT_RECYCLE_AFTER,
    ) -> Self:
        """Open a database connection and initialize its rebuild recycle gate.

        Args:
            connect: Factory used both for the initial connection and later reopenings.
            changes: The write record every table opened here reports to; the
                daemon shares one per physical database (``TableChanges.for_path``).
            recycle_after: Index rebuild count that arms connection recycling.

        Raises:
            OSError: The database connection factory cannot access its storage.
        """
        self = super().__new__(cls)
        self._inner = connect()
        self._gate = RecycleGate(recycle_after)
        self._reconnector = Reconnector(connect, self._gate)
        self._changes = changes
        return self

    @classmethod
    def shared(cls, connect: Callable[[], LanceDB], path: Path) -> Self:
        """Open *connect*, sharing the write record of the database at *path*."""
        return cls(connect, TableChanges.for_path(path))

    @classmethod
    def changes_of(cls, db: LanceDB) -> TableChanges:
        """Return the write record of *db*, or a fresh one if it is not a connection.

        A fresh record is dirty and never finalized, so a database whose writes
        nothing reports (a bare LanceDB handle) is rewritten on every finalize.
        That is the safe default when the gate cannot know.
        """
        return db.changes if isinstance(db, cls) else TableChanges()

    @classmethod
    def optimize_lock_of(cls, db: LanceDB, lancedb_path: Path) -> threading.RLock:
        """Return the one optimize lock of the physical database *db* opens.

        A connection takes it from its own record.  A bare handle has no record to
        share, so it takes the lock of the record at *lancedb_path*, which every
        other bare handle on that path takes too: two optimizes of one table never
        hold different locks.  Only the lock is shared that way; the write gate
        still treats a bare handle as always dirty (:meth:`changes_of`).
        """
        if isinstance(db, cls):
            return db.changes.optimizing()
        return TableChanges.for_path(lancedb_path).optimizing()

    @property
    def changes(self) -> TableChanges:
        """The record of writes made through this connection's tables."""
        return self._changes

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
        try:
            table = self._inner.create_table(name, data=data, schema=schema)
        finally:
            self._changes.note_write()
        return RecyclingTable(table, self)

    def note_index_rebuild(self) -> None:
        """Record one ``replace=True`` index rebuild; arm recycling at the cap.

        Recycling is deferred to the next connection boundary
        (:meth:`open_table`, :meth:`list_tables`, or :meth:`create_table`) so it
        never swaps mid-operation. The deferral does not gate on live table
        handles: handles already handed out keep working against the old
        connection after the swap.
        """
        self._gate.note_rebuild()

    def _maybe_recycle(self) -> None:
        """Reopen the underlying connection when a recycle is armed."""
        self._reconnector.reopen(self._install)

    def _install(self, fresh: LanceDB) -> None:
        self._inner = fresh
