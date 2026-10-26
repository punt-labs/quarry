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
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.ingest_unit import IngestUnit
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
    listener, on the loop.  A teardown that must not leave a store unread waits
    for :meth:`observed`, and names what it could not wait for with
    :meth:`describe`.

    A store runs on :meth:`threads`, a pool of its own.  The spool writes of an
    abort run on the shared pool, so a store that never returns can never keep
    one from starting.
    """

    # One bound for the whole daemon.  Only one store runs at a time (the embed
    # gate), so the rest of the bound is for stores a cancelled worker left
    # running.
    STORE_THREADS: ClassVar[int] = 8

    # Made on first use, under the lock; the pool does not exist until a store does.
    _threads: ClassVar[DaemonThreads | None] = None  # None: no store has run yet
    _threads_lock: ClassVar[threading.Lock] = threading.Lock()

    __slots__ = ("_detached", "_label", "_listeners", "_observed", "_state", "_task")

    _detached: bool
    _label: str
    _listeners: list[Callable[[JobLifecycle.Observed], None]]
    _observed: asyncio.Event
    _state: TaskState
    _task: asyncio.Future[Settlement]

    def __new__(
        cls,
        job: IngestUnit,
        state: TaskState,
        call: Callable[[], Settlement],
    ) -> Self:
        self = super().__new__(cls)
        self._detached = False
        self._label = (
            f"{state.kind} {type(job).__name__} for {job.collection} "
            f"(task {state.task_id})"
        )
        self._listeners = []
        self._observed = asyncio.Event()
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
        job: IngestUnit,
        state: TaskState,
        call: Callable[[], R],
        settle: Callable[[R], Settlement],
    ) -> Self:
        """Start *call* on a store thread for *job* and keep it on *state*.

        *settle* turns the result into a :class:`Settlement`; it runs on the
        thread, so a store settled after its worker is gone is settled by the
        same rule as one the worker waits for.
        """
        run = cls(job, state, lambda: settle(call()))
        state.store = run
        return run

    async def wait(self) -> Settlement:
        """Return how the store ended, or raise what it raised.

        A cancel of the waiter does not cancel the store: it ends the wait only.
        """
        return await asyncio.shield(self._task)

    def outcome(self) -> Settlement | None:  # None: the thread still runs
        """Return how the store ended, or ``None`` while its thread still runs."""
        if not self._task.done() or self._task.cancelled():
            return None  # the thread runs, or its answer was never delivered
        if (failure := self._task.exception()) is not None:
            return self._failed(failure)
        return self._task.result()

    def detach(self) -> None:
        """Observe the store's end: its worker is gone and will not read it."""
        self._detached = True
        self._task.add_done_callback(self._observe)

    def listen(self, listener: Callable[[JobLifecycle.Observed], None]) -> None:
        """Call *listener* with what the observer changed, when the store ends."""
        self._listeners.append(listener)

    @property
    def is_unobserved(self) -> bool:
        """Return whether the worker is gone and the store's end is not yet read."""
        return self._detached and not self._observed.is_set()

    async def observed(self) -> None:
        """Wait until the observer has read the store's end (after :meth:`detach`)."""
        await self._observed.wait()

    def describe(self) -> str:
        """Return what the store belongs to, safe to log: its kind, job and task."""
        return self._label

    def _observe(self, task: asyncio.Future[Settlement]) -> None:
        """Done callback, on the loop: settle the job from its detached store."""
        try:
            settlement = self._read(task)
            if settlement is None:
                return
            observed = JobLifecycle(self._state).observe_store(settlement)
            for listener in self._listeners:
                listener(observed)
        finally:
            self._observed.set()

    def _read(
        self, task: asyncio.Future[Settlement]
    ) -> Settlement | None:  # None: the store never ended
        """Return how *task*'s store ended, logging it if it failed or never ended.

        A store the loop's close cancelled never ended as far as anyone can tell;
        it is named, because a thread may still be inserting.  A refusal is an
        expected answer, logged without a traceback; anything else is a fault,
        logged by class and frames.
        """
        if task.cancelled():
            logger.warning(
                "%s: store still running at shutdown; its spool record "
                "(if any) may duplicate stored content",
                self._label,
            )
            return None
        failure = task.exception()
        if failure is None:
            return task.result()
        settlement = self._failed(failure)
        if isinstance(failure, RefusalError):
            logger.warning("%s: store refused: %s", self._label, settlement.reason)
        elif isinstance(failure, Exception):
            logger.error(
                "%s: store failed", self._label, exc_info=RedactedFailure.of(failure)
            )
        else:
            logger.error("%s: store failed: %s", self._label, settlement.reason)
        return settlement

    @staticmethod
    def _failed(failure: BaseException) -> Settlement:
        """Return the settlement for a store that raised *failure*."""
        if isinstance(failure, Exception):
            return Settlement.failed(FailureReason.of(failure))
        return Settlement.failed(f"internal error: {type(failure).__name__}")
