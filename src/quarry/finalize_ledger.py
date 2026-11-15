"""Per-database record of consecutive finalize-optimize failures, with backoff."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.api.health import FinalizeDatabaseHealth

if TYPE_CHECKING:
    from collections.abc import Callable

BACKOFF_BASE_SECONDS: float = 600.0
BACKOFF_CAP_SECONDS: float = 6 * 3600.0
# Keeps a pathological multi-day failure log from storing an unbounded string.
_MAX_REASON_CHARS = 500


@final
class FinalizeLedger:
    """Track one database's optimize failures and say when to try again.

    A failure doubles the wait before the next attempt (capped); a success
    clears the record.  Finalize runs off-thread while health reads from the
    event loop, so every transition holds the lock.
    """

    __slots__ = (
        "_clock",
        "_consecutive_failures",
        "_database",
        "_last_error",
        "_lock",
        "_retry_at",
    )

    _database: str
    _clock: Callable[[], float]
    _lock: threading.Lock
    _consecutive_failures: int
    _last_error: str
    _retry_at: float

    def __new__(
        cls, database: str, clock: Callable[[], float] = time.monotonic
    ) -> Self:
        self = super().__new__(cls)
        self._database = database
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._last_error = ""
        self._retry_at = 0.0
        return self

    def is_backed_off(self) -> bool:
        """Return whether the next optimize attempt should be skipped."""
        with self._lock:
            return self._consecutive_failures > 0 and self._clock() < self._retry_at

    def record_failure(self, reason: str) -> None:
        """Count a failed optimize and push the next attempt out exponentially."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = reason[:_MAX_REASON_CHARS]
            delay = min(
                BACKOFF_BASE_SECONDS * 2 ** (self._consecutive_failures - 1),
                BACKOFF_CAP_SECONDS,
            )
            self._retry_at = self._clock() + delay

    def record_success(self) -> None:
        """Clear the failure record after an optimize that completed."""
        with self._lock:
            self._consecutive_failures = 0
            self._last_error = ""
            self._retry_at = 0.0

    def health(self) -> FinalizeDatabaseHealth:
        """Return this database's ledger in the wire shape."""
        with self._lock:
            waiting = self._consecutive_failures > 0
            retry_in = max(0.0, self._retry_at - self._clock()) if waiting else 0.0
            return FinalizeDatabaseHealth(
                database=self._database,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
                retry_in_seconds=round(retry_in, 1),
            )


@final
class FinalizeLedgers:
    """The daemon's ledgers, one per database path, shared by finalize and health."""

    __slots__ = ()

    _ledgers: ClassVar[dict[str, FinalizeLedger]] = {}
    _guard: ClassVar[threading.Lock] = threading.Lock()

    @classmethod
    def for_database(cls, database: str) -> FinalizeLedger:
        """Return the ledger for *database*, creating it on first use."""
        with cls._guard:
            return cls._ledgers.setdefault(database, FinalizeLedger(database))

    @classmethod
    def snapshot(cls) -> tuple[FinalizeDatabaseHealth, ...]:
        """Return every database's ledger, ordered by database path."""
        with cls._guard:
            ledgers = sorted(cls._ledgers.items())
        return tuple(ledger.health() for _, ledger in ledgers)
