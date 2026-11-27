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
    it read, and only once the rewrite succeeded.  So a failed finalize leaves
    the database dirty, and a write that lands while a finalize runs leaves it
    dirty afterwards.  A database no finalize has covered in this process is dirty
    too: a finalize is not persisted, so after a crash nothing else knows whether
    the table still needs one.

    The record also keeps when the slow-clock maintenance last ran (cleanup of
    old table versions, the orphan-page sweep), which a quiet database still owes
    on a schedule.

    The store thread finalizes while the event loop and other store threads write,
    so every transition holds the lock.  One record exists per physical database,
    shared by every connection to it (:meth:`for_path`).
    """

    MAINTENANCE_INTERVAL_S: ClassVar[float] = 3600.0

    __slots__ = ("_clean", "_clock", "_generation", "_lock", "_maintained")

    _records: ClassVar[dict[Path, TableChanges]] = {}
    _guard: ClassVar[threading.Lock] = threading.Lock()

    _clock: Callable[[], float]
    _lock: threading.Lock
    _generation: int
    _clean: int | None  # None: no finalize has covered this database in this process
    _maintained: float | None  # None: slow-clock maintenance has not run yet

    def __new__(cls, clock: Callable[[], float] = time.monotonic) -> Self:
        """Start a record that needs a finalize and is due its first maintenance."""
        self = super().__new__(cls)
        self._clock = clock
        self._lock = threading.Lock()
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

    def mark_maintained(self) -> None:
        """Record that the slow-clock maintenance just ran."""
        with self._lock:
            self._maintained = self._clock()
