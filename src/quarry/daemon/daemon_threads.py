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

    Spooling an aborted job, and tearing a watch loop down, sync files and join
    threads, and a stalled disk can hold either for as long as it likes.  The
    default executor's threads, and anyio's, are joined when ``asyncio.run`` shuts
    down and again when the interpreter exits, so one stuck call would keep the
    process alive past its service stop timeout.  These threads are daemon
    threads on a private queue: nothing joins them, so the process exits whatever
    they are doing.

    At most *max_threads* run at once, started on demand and only when none is
    idle, so a burst of work uses up to the bound and one stalled call never
    keeps the rest from running.  Work runs in the order it was submitted, so a
    pool of one is a serial queue.  A result is handed back to the caller's loop
    with ``call_soon_threadsafe``, and is dropped if that loop has closed by then.
    """

    # The shared pool's bound: small, because a spool write is a few syncs.
    SHARED_THREADS: ClassVar[int] = 4

    # One pool for the process, made on first use so importing costs nothing.
    _shared: ClassVar[DaemonThreads | None] = None
    _shared_lock: ClassVar[threading.Lock] = threading.Lock()

    __slots__ = ("_idle", "_lock", "_max", "_name", "_started", "_work")

    _idle: int
    _lock: threading.Lock
    _max: int
    _name: str
    _started: int
    _work: queue.SimpleQueue[Callable[[], None]]

    def __new__(cls, max_threads: int = SHARED_THREADS, name: str = "quarry") -> Self:
        self = super().__new__(cls)
        self._idle = self._started = 0
        self._lock = threading.Lock()
        self._max = max_threads
        self._name = name
        self._work = queue.SimpleQueue()
        return self

    @classmethod
    def shared(cls) -> DaemonThreads:
        """Return the process's one shared pool, spool writes and sweeps."""
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls(name="quarry-spool")
            return cls._shared

    async def run[T](self, call: Callable[[], T]) -> T:
        """Run *call* on a daemon thread; return its result or raise its error."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()

        def work() -> None:
            try:
                result = call()
            except BaseException as exc:
                # Whatever it was, the caller is told: the future never hangs.
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
        """Queue *work*; claim an idle thread for it, or start one if the bound allows.

        The claim is made here, under the lock, so two submits can never count on
        the same idle thread.
        """
        with self._lock:
            self._work.put(work)
            if self._idle > 0:
                self._idle -= 1
            elif self._started < self._max:
                self._started += 1
                threading.Thread(
                    target=self._serve, name=self._name, daemon=True
                ).start()

    def _serve(self) -> None:
        """Take work until the process ends; a thread is idle only between jobs."""
        try:
            while True:
                with self._lock:
                    try:
                        work: Callable[[], None] | None = self._work.get_nowait()
                    except queue.Empty:
                        work = None
                        self._idle += 1
                if work is None:
                    work = self._work.get()  # the submit that woke us claimed us
                work()
        finally:
            with self._lock:
                self._started -= 1
