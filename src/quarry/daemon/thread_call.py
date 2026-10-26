"""Wait on the event loop from another thread, but never for good."""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from typing import TYPE_CHECKING, Self, final

from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from quarry.stop_signal import StopSignal

# How often the waiting thread looks up to see whether it was told to stop.
_POLL_S = 0.5


class ThreadCallAbortedError(RefusalError):
    """The wait ended without an answer; the message says why."""


@final
class ThreadCall:
    """Run a coroutine on the loop and wait for it from a worker thread.

    The wait ends with the coroutine's result or exception, or, never later than
    *total_s*, in :class:`ThreadCallAbortedError`: because the loop is closed, is
    cancelling the coroutine (the daemon is shutting down), has stopped
    answering, or because the caller's stop signal was set, which is honored
    within a poll rather than when the whole wait runs out.
    """

    __slots__ = ("_loop", "_stop", "_total_s")

    _loop: asyncio.AbstractEventLoop
    _stop: StopSignal
    _total_s: float

    def __new__(
        cls, loop: asyncio.AbstractEventLoop, stop: StopSignal, total_s: float
    ) -> Self:
        self = super().__new__(cls)
        self._loop = loop
        self._stop = stop
        self._total_s = total_s
        return self

    @classmethod
    def here(cls, stop: StopSignal, total_s: float) -> Self:
        """Return a call onto the running event loop (call it from that loop)."""
        return cls(asyncio.get_running_loop(), stop, total_s)

    def result[T](self, coro: Coroutine[object, object, T]) -> T:
        """Return *coro*'s result, run on the loop; raise if the wait cannot finish."""
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as exc:  # the loop is closed
            coro.close()
            msg = "event loop closed"
            raise ThreadCallAbortedError(msg) from exc
        deadline = time.monotonic() + self._total_s
        while not concurrent.futures.wait((future,), timeout=_POLL_S).done:
            self._give_up_if_due(future, deadline)
        if future.cancelled():
            msg = "daemon shutting down"
            raise ThreadCallAbortedError(msg)
        return future.result()

    def _give_up_if_due[T](
        self, future: concurrent.futures.Future[T], deadline: float
    ) -> None:
        """Cancel *future* and raise if the caller was told to stop or time ran out."""
        if self._stop.is_set():
            future.cancel()
            raise ThreadCallAbortedError(self._stop.reason)
        if time.monotonic() >= deadline:
            future.cancel()
            msg = "event loop not responding"
            raise ThreadCallAbortedError(msg)
