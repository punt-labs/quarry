"""Run blocking calls on daemon threads that nothing joins at exit."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from dataclasses import dataclass
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
    _work: queue.SimpleQueue[_Work]

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
        """Run *call* on a daemon thread; return its result or raise its error.

        Whatever *call* raises reaches the caller as an exception it can hold: a
        ``BaseException`` that is not an ``Exception`` (``SystemExit``, a cancel)
        and ``StopIteration`` (which a future refuses) arrive as
        :class:`DaemonThreadAbortedError`, chained to the original.  The thread then
        ends on a ``BaseException``, and the pool starts another when it needs one.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()

        def work() -> None:
            try:
                result = call()
            except BaseException as exc:
                # Hand back first: nothing after this can strand the caller.
                self._hand_back(loop, partial(self._fail, future, self._holdable(exc)))
                logger.debug(
                    "daemon thread call failed", exc_info=RedactedFailure.of(exc)
                )
                if not isinstance(exc, Exception):
                    raise
            else:
                self._hand_back(loop, partial(self._succeed, future, result))

        def abandon(failure: BaseException) -> None:
            self._hand_back(loop, partial(self._fail, future, failure))

        self._submit(_Work(work, abandon))
        return await future

    @staticmethod
    def _holdable(exc: BaseException) -> BaseException:
        """Return what a future can carry for *exc*: itself, or an aborted wrapper."""
        if isinstance(exc, Exception) and not isinstance(exc, StopIteration):
            return exc
        wrapper = DaemonThreadAbortedError(f"the call ended with {type(exc).__name__}")
        wrapper.__cause__ = exc
        return wrapper

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

    def _submit(self, work: _Work) -> None:
        """Run *work* on an idle thread, a new one if the bound allows, else queue it.

        The idle claim is made here, under the lock, so two submits can never
        count on the same idle thread.  A new thread is counted only once it has
        started, and gets *work* directly, so a failed start leaves nothing
        queued and nothing counted: the error reaches the caller.
        """
        with self._lock:
            if self._idle > 0:
                self._idle -= 1
                self._work.put(work)
            elif self._started < self._max:
                threading.Thread(
                    target=self._serve, args=(work,), name=self._name, daemon=True
                ).start()
                self._started += 1
            else:
                self._work.put(work)

    def _serve(self, work: _Work) -> None:
        """Run *work*, then whatever comes next, until the process ends."""
        try:
            while True:
                work.call()
                work = self._next()
        finally:
            with self._lock:
                self._started -= 1
                self._replace_if_waiting()

    def _replace_if_waiting(self) -> None:
        """Start a thread for queued work a thread that just ended would have run.

        Called with the lock held.  Work queued while every thread was busy is
        served by whichever finishes; one that ends instead (``SystemExit``)
        must hand it on.  If no thread can be started for it, its caller is told
        why, first in, first failed, rather than left waiting on a queue nothing
        serves; and when no thread is left at all, everything behind it is stranded
        the same way (a later submit starts a thread, or fails, as usual).
        """
        if self._idle > 0 or self._started >= self._max:
            return
        try:
            work = self._work.get_nowait()
        except queue.Empty:
            return
        try:
            threading.Thread(
                target=self._serve, args=(work,), name=self._name, daemon=True
            ).start()
        except RuntimeError as exc:
            logger.warning("daemon threads: could not replace an ended thread")
            work.abandon(exc)
            self._abandon_all_if_unserved(exc)
        else:
            self._started += 1

    def _abandon_all_if_unserved(self, failure: BaseException) -> None:
        """Fail every queued call, in order, if no thread is left to serve them."""
        if self._started > 0:
            return
        while True:
            try:
                stranded = self._work.get_nowait()
            except queue.Empty:
                return
            stranded.abandon(failure)

    def _next(self) -> _Work:
        """Return the next job; a thread is idle only while it waits for one."""
        with self._lock:
            try:
                return self._work.get_nowait()
            except queue.Empty:
                self._idle += 1
        return self._work.get()  # the submit that woke us has claimed us


class DaemonThreadAbortedError(RuntimeError):
    """A call on a daemon thread ended in a way a future cannot carry."""


@final
@dataclass(frozen=True, slots=True)
class _Work:
    """One queued call: what a thread runs, and how its caller is told it never will."""

    call: Callable[[], None]
    abandon: Callable[[BaseException], None]
