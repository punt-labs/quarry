"""A stop request that remembers why it was made."""

from __future__ import annotations

import threading
from typing import Self, final


@final
class StopSignal:
    """An event one thread sets and another polls, with the reason it was set.

    The first reason wins: a run stopped by a daemon shutdown that is then
    cancelled still says it was the shutdown.
    """

    __slots__ = ("_event", "_lock", "_reason")

    _event: threading.Event
    _lock: threading.Lock
    _reason: str

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        return self

    def set(self, reason: str) -> None:
        """Request the stop for *reason*; a reason already given is kept."""
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    def is_set(self) -> bool:
        """Return whether a stop has been requested."""
        return self._event.is_set()

    @property
    def reason(self) -> str:
        """Return why the stop was requested, or ``""`` if it was not."""
        return self._reason
