"""A watch-stop cleanup that runs on a daemon thread and notes when it begins."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.daemon_threads import DaemonThreads


@final
class ThreadStep:
    """Run *call* on *pool*, and remember whether it has really begun.

    On a serial pool a step starts only after the one before it ends, so a stuck
    step leaves the later ones queued and not started: :meth:`queued_behind` says
    behind whom, so a report can tell "never started" from "still running".
    """

    __slots__ = ("_behind", "_call", "_pool", "_started")

    _behind: str | None  # None: the first step, which nothing precedes
    _call: Callable[[], None]
    _pool: DaemonThreads
    _started: threading.Event

    def __new__(
        cls,
        pool: DaemonThreads,
        call: Callable[[], None],
        behind: str | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._pool = pool
        self._call = call
        self._behind = behind
        self._started = threading.Event()
        return self

    async def run(self) -> None:
        """Run the call on the pool's thread."""
        await self._pool.run(self._begin)

    def queued_behind(self) -> str | None:  # None: it has begun, or nothing precedes it
        """Return the step this one is waiting behind, if it has not begun."""
        return None if self._started.is_set() else self._behind

    def _begin(self) -> None:
        """Note that the step has begun, then run it."""
        self._started.set()
        self._call()
