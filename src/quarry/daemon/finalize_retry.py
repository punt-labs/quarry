"""Re-request a finalize that left its database dirty, once it may run again."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable


@final
class FinalizeRetry:
    """One pending retry per database for a finalize that did not settle clean.

    A finalize that decided to rewrite cancels the armed trailing finalize, so if
    it then fails or backs off nothing else is scheduled, and with the periodic
    safety scan disabled nothing ever would be.  The retry waits out the
    ledger's back-off, then calls *request*, which goes back through the
    finalize throttle so it still honors the rate limit.  A retry already
    pending for the database is kept.  State lives on the event loop.
    """

    __slots__ = ("_loop", "_timers")

    _loop: asyncio.AbstractEventLoop
    _timers: dict[str, asyncio.TimerHandle]

    def __new__(cls, loop: asyncio.AbstractEventLoop) -> Self:
        """Start with no retry pending."""
        self = super().__new__(cls)
        self._loop = loop
        self._timers = {}
        return self

    def arm(self, database: str, delay_s: float, request: Callable[[], None]) -> None:
        """Schedule *request* in *delay_s* seconds unless a retry is pending."""
        if database not in self._timers:
            self._timers[database] = self._loop.call_later(
                delay_s, self._fire, database, request
            )

    def cancel_all(self) -> None:
        """Cancel every pending retry (loop shutdown)."""
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()

    def _fire(self, database: str, request: Callable[[], None]) -> None:
        self._timers.pop(database, None)
        request()
