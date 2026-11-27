"""The record of whether a database's table changed since its last finalize."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, ClassVar, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@final
class TableChanges:
    """Track one physical database: writes since its last finalize, and its slow clock.

    Every write bumps a generation counter.  A finalize reads the counter
    *before* it rewrites the table and marks the database clean only up to what
    it read, and only once the rewrite succeeded, including its full-text
    rebuild.  So a failed finalize leaves the database dirty, and a write that
    lands while a finalize runs leaves it dirty afterwards.  The write is counted
    after it ends, even when it raised, because a failed write may have applied.
    A database no finalize has covered in this process is dirty too: a finalize is
    not persisted, so after a crash nothing else knows whether the table still
    needs one.

    The record also keeps when the slow-clock maintenance last ran (the orphan-page
    sweep), which a quiet database still owes on a schedule.  The sweep's own
    page deletes are writes, so a sweep that deletes pages dirties the record and
    costs one extra rewrite.

    The store thread finalizes while the event loop and other store threads write,
    so every transition holds the lock.  One record exists per physical database,
    shared by every connection to it (:meth:`for_path`).
    """

    MAINTENANCE_INTERVAL_S: ClassVar[float] = 3600.0

    __slots__ = (
        "_clean",
        "_clock",
        "_generation",
        "_lock",
        "_maintained",
        "_optimizing",
    )

    _records: ClassVar[dict[Path, TableChanges]] = {}
    _guard: ClassVar[threading.Lock] = threading.Lock()

    _clock: Callable[[], float]
    _lock: threading.Lock
    _optimizing: threading.RLock
    _generation: int
    _clean: int | None  # None: no finalize has covered this database in this process
    _maintained: float | None  # None: slow-clock maintenance has not run yet

    def __new__(cls, clock: Callable[[], float] = time.monotonic) -> Self:
        """Start a record that needs a finalize and is due its first maintenance."""
        self = super().__new__(cls)
        self._clock = clock
        self._lock = threading.Lock()
        self._optimizing = threading.RLock()
        self._generation = 0
        self._clean = None
        self._maintained = None
        return self

    @classmethod
    def for_path(cls, lancedb_path: Path) -> TableChanges:
        """Return the record of the database at *lancedb_path*, creating it.

        Keyed by the resolved path, so a symlinked alias and every connection to
        the same directory share one record.
        """
        key = lancedb_path.resolve()
        with cls._guard:
            return cls._records.setdefault(key, cls())

    def optimizing(self) -> threading.RLock:
        """Return the lock that keeps two optimizes of this database from overlapping.

        The finalize rewrite and the operator's manual optimize both take it, so
        neither compacts or rebuilds the index under the other, and neither
        clears or records a failure in the middle of the other's run.
        """
        return self._optimizing

    def note_write(self) -> None:
        """Record a write that has completed (insert, delete, purge, page write)."""
        with self._lock:
            self._generation += 1

    def snapshot(self) -> int:
        """Return the generation a finalize about to start must later report clean."""
        with self._lock:
            return self._generation

    def needs_rewrite(self) -> bool:
        """Return whether the table changed since the last successful finalize."""
        with self._lock:
            return self._clean is None or self._clean < self._generation

    def mark_clean(self, snapshot: int) -> None:
        """Record that a successful finalize covered every write up to *snapshot*."""
        with self._lock:
            if self._clean is None or snapshot > self._clean:
                self._clean = snapshot

    def maintenance_due(self) -> bool:
        """Return whether the slow-clock maintenance is due."""
        with self._lock:
            return (
                self._maintained is None
                or self._clock() - self._maintained >= self.MAINTENANCE_INTERVAL_S
            )

    def mark_maintenance_failed(self) -> None:
        """Make the slow-clock maintenance due again, whatever succeeded before."""
        with self._lock:
            self._maintained = None

    def mark_maintained(self) -> None:
        """Record that the slow-clock maintenance just ran."""
        with self._lock:
            self._maintained = self._clock()
