"""Run blocking calls on daemon threads that nothing joins at exit."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from functools import partial
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@final
class DaemonThreads:
    """A small pool of daemon threads for work that may never return.

    Spooling an aborted job syncs files, and a stalled disk can hold a sync for
    as long as it likes.  The default executor's threads are joined when
    ``asyncio.run`` shuts down and again when the interpreter exits, so one stuck
    sync would keep the process alive past its service stop timeout.  These
    threads are daemon threads on a private queue: nothing joins them, so the
    process exits whatever they are doing.

    At most :attr:`MAX_THREADS` run at once, started on demand; more work waits
    its turn.  The bound keeps a flood of aborts from starting a thread per job,
    and is small because a spool write is a few syncs.  A result is handed back
    to the caller's loop with ``call_soon_threadsafe``, and is dropped if that
    loop has closed by then.
    """

    MAX_THREADS: ClassVar[int] = 4

    _shared: ClassVar[DaemonThreads | None] = None

    __slots__ = ("_idle", "_lock", "_started", "_work")

    _idle: int
    _lock: threading.Lock
    _started: int
    _work: queue.SimpleQueue[Callable[[], None]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._idle = self._started = 0
        self._lock = threading.Lock()
        self._work = queue.SimpleQueue()
        return self

    @classmethod
    def shared(cls) -> DaemonThreads:
        """Return the process's one pool."""
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    async def run[T](self, call: Callable[[], T]) -> T:
        """Run *call* on a daemon thread; return its result or raise its error."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()

        def work() -> None:
            try:
                result = call()
            except Exception as exc:
                logger.debug(
                    "daemon thread call failed", exc_info=RedactedFailure.of(exc)
                )
                self._hand_back(loop, partial(self._fail, future, exc))
            else:
                self._hand_back(loop, partial(self._succeed, future, result))

        self._submit(work)
        return await future

    @staticmethod
    def _succeed[T](future: asyncio.Future[T], result: T) -> None:
        """Complete *future* with *result*, unless its caller gave up."""
        if not future.cancelled():
            future.set_result(result)

    @staticmethod
    def _fail[T](future: asyncio.Future[T], failure: BaseException) -> None:
        """Complete *future* with *failure*, unless its caller gave up."""
        if not future.cancelled():
            future.set_exception(failure)

    @staticmethod
    def _hand_back(
        loop: asyncio.AbstractEventLoop, deliver: Callable[[], None]
    ) -> None:
        """Run *deliver* on *loop*; a closed loop has no one left to tell."""
        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            return

    def _submit(self, work: Callable[[], None]) -> None:
        """Queue *work*, starting a thread if none is idle and the bound allows."""
        with self._lock:
            self._work.put(work)
            if self._idle == 0 and self._started < self.MAX_THREADS:
                self._started += 1
                threading.Thread(
                    target=self._serve, name="quarry-spool", daemon=True
                ).start()

    def _serve(self) -> None:
        """Take work until the process ends."""
        while True:
            with self._lock:
                self._idle += 1
            work = self._work.get()
            with self._lock:
                self._idle -= 1
            work()
