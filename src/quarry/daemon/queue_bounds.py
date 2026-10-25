"""The bounds of an ingest queue: how deep it may be, and which workers live."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from time import monotonic
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.collection_worker import CollectionWorker

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Generator

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.route_key import RouteKey


@final
class QueueGate:
    """Admit jobs up to a depth, wake whoever waits for room, and signal a close.

    Admission is non-blocking (a full queue is the caller's 503); a producer that
    is willing to wait registers with :meth:`wait_released` and is woken when a
    job leaves.  A producer may also keep *reserve* slots back for traffic that
    must not be shed (a hook's capture, against a backfill).  Closing stops
    admission and sets every registered stop event, so a producer on another
    thread learns of the shutdown without polling the queue.
    """

    __slots__ = ("_closing", "_depth", "_max_depth", "_stops", "_waiters")

    _closing: bool
    _depth: int
    _max_depth: int
    _stops: set[threading.Event]
    _waiters: list[asyncio.Future[None]]

    def __new__(cls, max_depth: int) -> Self:
        self = super().__new__(cls)
        self._closing = False
        self._depth = 0
        self._max_depth = max_depth
        self._stops = set()
        self._waiters = []
        return self

    @property
    def closing(self) -> bool:
        """Return whether the queue has stopped admitting."""
        return self._closing

    def has_room(self, reserve: int = 0) -> bool:
        """Return whether a slot is free once *reserve* slots are kept back.

        At least one slot is always usable, so a reserve larger than the queue
        cannot lock a patient producer out for good.  A look, not a take: pair it
        with :meth:`try_take` in one synchronous step.
        """
        ceiling = max(1, self._max_depth - reserve)
        return not self._closing and self._depth < ceiling

    def try_take(self) -> bool:
        """Take one admission slot; ``False`` if the queue is closed or full."""
        if not self.has_room():
            return False
        self._depth += 1
        return True

    def release(self) -> None:
        """Free one admission slot and wake every producer waiting for room."""
        self._depth -= 1
        self._wake()

    async def wait_released(self, timeout: float) -> None:
        """Wait up to *timeout* seconds for a released slot or the gate closing."""
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            with suppress(TimeoutError):
                async with asyncio.timeout(timeout):
                    await waiter
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    def close(self) -> None:
        """Stop admitting, signal every registered stop event, wake the waiters."""
        self._closing = True
        for event in self._stops:
            event.set()
        self._wake()

    @contextmanager
    def stopping(self, event: threading.Event) -> Generator[None]:
        """Set *event* when the gate closes, for as long as the block runs."""
        self._stops.add(event)
        if self._closing:
            event.set()
        try:
            yield
        finally:
            self._stops.discard(event)

    def _wake(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)


@final
class WorkerPool:
    """One FIFO worker per route key, with the map itself bounded.

    The route key is client-controlled, so resident workers must not accrue: idle
    workers are reaped, and the map is capped at ``ingest_max_workers``.  Reaping
    runs synchronously, so no submit or worker can interleave between the idle
    check and the removal, and a reaped key is recreated lazily on its next
    submit as, again, the sole writer for that table.
    """

    __slots__ = (
        "_ctx",
        "_embed_gate",
        "_max_workers",
        "_reaped",
        "_release",
        "_worker_idle_s",
        "_workers",
    )

    _ctx: DaemonContext
    _embed_gate: asyncio.Semaphore
    _release: Callable[[], None]
    _max_workers: int
    _worker_idle_s: float
    _workers: dict[RouteKey, CollectionWorker]
    # Cancelled worker tasks retained until their cancellation completes: asyncio
    # keeps only a weak reference, so a reaped worker's still-pending task would
    # be garbage-collected mid-flight ("Task was destroyed but it is pending")
    # without a strong reference held here until the done-callback discards it.
    _reaped: set[asyncio.Task[None]]

    def __new__(
        cls,
        ctx: DaemonContext,
        embed_gate: asyncio.Semaphore,
        release: Callable[[], None],
    ) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._embed_gate = embed_gate
        self._release = release
        self._max_workers = ctx.settings.ingest_max_workers
        self._worker_idle_s = ctx.settings.ingest_worker_idle_s
        self._workers = {}
        self._reaped = set()
        return self

    def __len__(self) -> int:
        return len(self._workers)

    def retains_tasks(self) -> bool:
        """Return whether a cancelled worker task is still running out."""
        return bool(self._reaped)

    def workers(self) -> list[CollectionWorker]:
        """Return the resident workers."""
        return list(self._workers.values())

    def is_alive(self, key: RouteKey) -> bool:
        """Return whether *key* has a worker whose task is still running."""
        worker = self._workers.get(key)
        return worker is not None and worker.is_alive()

    def for_key(self, key: RouteKey) -> CollectionWorker | None:
        """Return *key*'s worker, reaping idle ones and honoring the cap.

        ``None`` means the cap is full of busy route keys and the caller must
        shed load (503).
        """
        self._reap_idle(exclude=key)
        worker = self._workers.get(key)
        if worker is not None:
            return worker
        if len(self._workers) >= self._max_workers and not self._evict_reapable():
            return None
        worker = CollectionWorker(self._ctx, self._embed_gate, self._release)
        self._workers[key] = worker
        return worker

    def abort(self) -> None:
        """Abort every worker, retaining each task until its cancellation completes."""
        for worker in self._workers.values():
            self._retain(worker.abort())

    def _reap_idle(self, *, exclude: RouteKey) -> None:
        """Drop workers idle past the reap interval so client keys can't accrue."""
        now = monotonic()
        stale = [
            key
            for key, worker in self._workers.items()
            if key != exclude
            and worker.is_reapable()
            and worker.idle_seconds(now) >= self._worker_idle_s
        ]
        for key in stale:
            self._retain(self._workers.pop(key).shutdown())

    def _evict_reapable(self) -> bool:
        """Drop one idle worker to free a cap slot; ``False`` if all are busy."""
        victim = next(
            (k for k, worker in self._workers.items() if worker.is_reapable()), None
        )
        if victim is None:
            return False
        self._retain(self._workers.pop(victim).shutdown())
        return True

    def _retain(self, task: asyncio.Task[None]) -> None:
        """Hold a cancelled worker task until its cancellation runs out.

        The done-callback drops it once it finishes, the synchronous counterpart
        of ``aclose`` awaiting ``worker.wait()`` under ``suppress(CancelledError)``.
        """
        self._reaped.add(task)
        task.add_done_callback(self._reaped.discard)
