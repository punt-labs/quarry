"""A watch-stop cleanup that runs on a daemon thread and notes when it begins."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from quarry.daemon.daemon_threads import DaemonThreads


@final
class ThreadStep:
    """Run *call* on *pool*, and remember whether it has begun and whether it ended.

    On a serial pool a step starts only after the one before it ends, so a stuck
    step leaves every later one queued and not started: :meth:`queued_behind`
    names the first of the *earlier* steps that has not ended, which is the stuck
    one, so a report can tell "never started" from "still running" and say who
    is holding the thread.
    """

    __slots__ = ("_call", "_earlier", "_ended", "_name", "_pool", "_started")

    _call: Callable[[], None]
    _earlier: Sequence[ThreadStep]
    _ended: threading.Event
    _name: str
    _pool: DaemonThreads
    _started: threading.Event

    def __new__(
        cls,
        pool: DaemonThreads,
        name: str,
        call: Callable[[], None],
        earlier: Sequence[ThreadStep] = (),
    ) -> Self:
        self = super().__new__(cls)
        self._pool = pool
        self._name = name
        self._call = call
        self._earlier = earlier
        self._started = threading.Event()
        self._ended = threading.Event()
        return self

    @property
    def name(self) -> str:
        """Return the step's name, as the report calls it."""
        return self._name

    async def run(self) -> None:
        """Run the call on the pool's thread."""
        await self._pool.run(self._begin)

    def queued_behind(self) -> str | None:  # None: it has begun, or nothing holds it
        """Return the earlier step that has not ended, if this one has not begun."""
        if self._started.is_set():
            return None
        return next((step.name for step in self._earlier if not step.has_ended), None)

    @property
    def has_ended(self) -> bool:
        """Return whether the call has returned (or raised)."""
        return self._ended.is_set()

    def _begin(self) -> None:
        """Note that the step has begun, run it, and note that it has ended."""
        self._started.set()
        try:
            self._call()
        finally:
            self._ended.set()
