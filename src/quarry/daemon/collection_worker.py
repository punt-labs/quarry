"""One collection's resident FIFO ingest worker and its queued-job message.

A :class:`CollectionWorker` is the per-collection serialization the queue is
built on: a single consumer coroutine draining one ``asyncio.Queue`` means only
one ``progressive_insert`` caller per LanceDB collection is ever in flight —
the single-writer precondition DES-034's ingest primitives assume.  Each unit
runs under the shared embed gate; its duration is bounded by the ingest work
itself — the web fetch enforces a socket timeout and the embed and LanceDB write
are finite — not by a coroutine-level deadline, which could not interrupt a
non-cancellable threadpool ingest anyway.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState


@final
@dataclass(frozen=True, slots=True)
class _Queued:
    """One admitted job paired with the task state it reports through."""

    job: IngestUnit
    state: TaskState


@final
class CollectionWorker:
    """One collection's FIFO worker: drains its queue under the shared embed gate.

    ``None`` on the queue is the drain sentinel; it never carries an admit slot,
    so it is not counted against the queue's admission bound.  The worker tracks
    whether it is running a job and when it last went idle so the queue can reap
    it once it has sat empty and idle past the configured interval.
    """

    __slots__ = (
        "_ctx",
        "_embed_gate",
        "_last_active",
        "_queue",
        "_release_admit",
        "_running",
        "_task",
    )

    _ctx: DaemonContext
    _embed_gate: asyncio.Semaphore
    _last_active: float
    _queue: asyncio.Queue[_Queued | None]
    _release_admit: Callable[[], None]
    _running: bool
    _task: asyncio.Task[None]

    def __new__(
        cls,
        ctx: DaemonContext,
        embed_gate: asyncio.Semaphore,
        release_admit: Callable[[], None],
    ) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._embed_gate = embed_gate
        self._queue = asyncio.Queue()
        self._release_admit = release_admit
        self._running = False
        self._last_active = monotonic()
        # Start the resident consumer now; try_submit only ever runs inside a
        # request handler, so a running loop is guaranteed at creation.
        self._task = asyncio.create_task(self._run())
        return self

    def enqueue(self, job: IngestUnit, state: TaskState) -> None:
        """Append a job (with its task state) to this FIFO queue (never blocks)."""
        self._queue.put_nowait(_Queued(job, state))

    def stop(self) -> None:
        """Enqueue the drain sentinel so the worker returns after its backlog."""
        self._queue.put_nowait(None)

    async def wait(self) -> None:
        """Await the worker's natural completion (after it drains to the sentinel)."""
        await self._task

    def is_reapable(self) -> bool:
        """Return whether the worker holds no work and runs no job (safe to drop)."""
        return not self._running and self._queue.empty()

    def idle_seconds(self, now: float) -> float:
        """Return seconds since the worker last finished a job (or was created)."""
        return now - self._last_active

    def shutdown(self) -> None:
        """Cancel an idle worker's task; the caller guarantees an empty queue."""
        self._task.cancel()

    def abort(self) -> None:
        """Cancel the worker and fail every still-queued job (recoverable).

        The in-flight job's own ``task_terminal`` records ``failed`` when the
        cancellation reaches it; the jobs still waiting never ran, so this marks
        them ``failed`` and frees their admit slots.  A durable capture artifact
        outlives a ``failed`` task and is recoverable via ``quarry backfill``.
        """
        self._task.cancel()
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is not None:
                item.state.status = "failed"
                item.state.error = "daemon shut down before ingest completed"
                self._release_admit()

    async def _run(self) -> None:
        """Drain jobs FIFO, one at a time, each under the shared embed gate."""
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            self._running = True
            try:
                item.state.status = "running"  # queued -> running on dequeue
                # No coroutine-level timeout: job.run offloads the ingest to a
                # non-cancellable threadpool thread, so asyncio.wait_for could
                # not interrupt a hang — it would await the thread anyway and
                # hold the gate for the whole hang.  The one genuinely unbounded
                # wait is the web fetch, which is bounded at its own socket
                # timeout (web_fetch.py); embed + LanceDB write are finite.
                async with self._embed_gate:  # global embed-concurrency bound
                    await item.job.run(self._ctx, item.state)  # records terminal
            finally:
                if item.state.status == "running":
                    # Cancelled (drain abort) while awaiting the embed gate, so
                    # job.run — and its task_terminal — never ran and never
                    # recorded a terminal status.  Record it here so the drain's
                    # "every dequeued job reaches a terminal state" holds for a
                    # job stuck between dequeue and gate acquisition.
                    item.state.status = "failed"
                    item.state.error = "daemon shut down before ingest completed"
                self._running = False
                self._last_active = monotonic()
                self._release_admit()
                self._queue.task_done()
