"""Count index rebuilds and decide when a connection is due to be reopened."""

from __future__ import annotations

import threading
from typing import Self, final


@final
class RecycleGate:
    """Arm after *recycle_after* rebuilds; admit one reopen at a time.

    :meth:`begin` claims the reopen, :meth:`succeed` disarms and resets the
    count, and :meth:`fail` releases the claim but leaves the gate armed so the
    next boundary retries.  A concurrent caller whose :meth:`begin` returns
    ``False`` keeps serving the still-valid old connection.
    """

    __slots__ = ("_armed", "_busy", "_lock", "_rebuilds", "_recycle_after")

    _armed: bool
    _busy: bool
    _lock: threading.Lock
    _rebuilds: int
    _recycle_after: int

    def __new__(cls, recycle_after: int) -> Self:
        self = super().__new__(cls)
        self._armed = False
        self._busy = False
        self._lock = threading.Lock()
        self._rebuilds = 0
        self._recycle_after = recycle_after
        return self

    @property
    def lock(self) -> threading.Lock:
        """The lock that makes the caller's swap atomic with :meth:`succeed`."""
        return self._lock

    def note_rebuild(self) -> None:
        """Record one ``replace=True`` index rebuild; arm at the cap."""
        with self._lock:
            self._rebuilds += 1
            if self._rebuilds >= self._recycle_after:
                self._armed = True

    def begin(self) -> bool:
        """Claim the reopen; return whether this caller should perform it."""
        with self._lock:
            if not self._armed or self._busy:
                return False
            self._busy = True
            return True

    def succeed(self) -> None:
        """Disarm and reset after a reopen; the caller holds :attr:`lock`."""
        self._armed = False
        self._rebuilds = 0
        self._busy = False

    def fail(self) -> None:
        """Release the claim, leaving the gate armed for the next boundary."""
        with self._lock:
            self._busy = False
