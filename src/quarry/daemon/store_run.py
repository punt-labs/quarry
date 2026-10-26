"""A job's store: kept by the job, and observed once its worker is gone."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.settlement import Settlement
from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class StoreRun:
    """The store thread of one job, and what becomes of its result.

    A store deletes the earlier copy and inserts the new one on a thread that no
    cancel stops.  When the worker is cancelled mid-store the job's status is
    settled from what is known then, and the thread keeps running: this class is
    the one who reads its result when it ends.  The job keeps the run on its
    state (:attr:`TaskState.store`), the worker awaits it (:meth:`wait`), and a
    cancelled worker hands it on (:meth:`detach`), after which the end of the
    thread settles the status (:meth:`JobLifecycle.observe_store`) and tells each
    listener, on the loop.

    A store runs on :meth:`threads`, a pool of its own.  The spool writes of an
    abort run on the shared pool, so a store that never returns can never keep
    one from starting.
    """

    # One bound for the whole daemon.  Only one store runs at a time (the embed
    # gate), so the rest of the bound is for stores a cancelled worker left
    # running.
    STORE_THREADS: ClassVar[int] = 8

    _threads: ClassVar[DaemonThreads | None] = None
    _threads_lock: ClassVar[threading.Lock] = threading.Lock()

    __slots__ = ("_listeners", "_state", "_task")

    _listeners: list[Callable[[JobLifecycle.Observed], None]]
    _state: TaskState
    _task: asyncio.Future[Settlement]

    def __new__(
        cls,
        state: TaskState,
        call: Callable[[], Settlement],
    ) -> Self:
        self = super().__new__(cls)
        self._listeners = []
        self._state = state
        self._task = asyncio.ensure_future(cls.threads().run(call))
        return self

    @classmethod
    def threads(cls) -> DaemonThreads:
        """Return the daemon's one store pool, never the shared spool pool."""
        with cls._threads_lock:
            if cls._threads is None:
                cls._threads = DaemonThreads(cls.STORE_THREADS, "quarry-store")
            return cls._threads

    @classmethod
    def start[R](
        cls,
        state: TaskState,
        call: Callable[[], R],
        settle: Callable[[R], Settlement],
    ) -> Self:
        """Start *call* on a store thread and keep it on *state*.

        *settle* turns the result into a :class:`Settlement`; it runs on the
        thread, so a store settled after its worker is gone is settled by the
        same rule as one the worker waits for.
        """
        run = cls(state, lambda: settle(call()))
        state.store = run
        return run

    async def wait(self) -> Settlement:
        """Return how the store ended, or raise what it raised.

        A cancel of the waiter does not cancel the store: it ends the wait only.
        """
        return await asyncio.shield(self._task)

    def outcome(self) -> Settlement | None:
        """Return how the store ended, or ``None`` while its thread still runs."""
        if not self._task.done() or self._task.cancelled():
            return None  # the thread runs, or its answer was never delivered
        if (failure := self._task.exception()) is not None:
            return self._failed(failure)
        return self._task.result()

    def detach(self) -> None:
        """Observe the store's end: its worker is gone and will not read it."""
        self._task.add_done_callback(self._observe)

    def listen(self, listener: Callable[[JobLifecycle.Observed], None]) -> None:
        """Call *listener* with what the observer changed, when the store ends."""
        self._listeners.append(listener)

    def _observe(self, task: asyncio.Future[Settlement]) -> None:
        """Done callback, on the loop: settle the job from its detached store."""
        if task.cancelled():
            return  # the loop is going away; nobody is left to tell
        if (failure := task.exception()) is not None:
            logger.error(
                "background %s store failed",
                self._state.kind,
                exc_info=RedactedFailure.of(failure),
            )
            settlement = self._failed(failure)
        else:
            settlement = task.result()
        observed = JobLifecycle(self._state).observe_store(settlement)
        for listener in self._listeners:
            listener(observed)

    @staticmethod
    def _failed(failure: BaseException) -> Settlement:
        """Return the settlement for a store that raised *failure*."""
        if isinstance(failure, Exception):
            return Settlement.failed(FailureReason.of(failure))
        return Settlement.failed(f"internal error: {type(failure).__name__}")
