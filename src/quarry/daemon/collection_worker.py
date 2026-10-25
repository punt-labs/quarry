"""One collection's resident FIFO ingest worker and its queued-job message.

A :class:`CollectionWorker` is the per-collection serialization the queue is
built on: a single consumer coroutine draining one ``asyncio.Queue`` means only
one ``DocumentStreamer.insert`` caller per LanceDB collection is ever in flight —
the single-writer precondition the ingest primitives assume.  Each unit
runs under the shared embed gate; its duration is bounded by the ingest work
itself — the web fetch enforces a total deadline + size cap and the embed and
LanceDB write are finite — not by a coroutine-level deadline, which could not
interrupt a non-cancellable threadpool ingest anyway.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.abort_reason import AbortReason
from quarry.daemon.job_lifecycle import JobLifecycle

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
        "_aborted",
        "_ctx",
        "_embed_gate",
        "_last_active",
        "_queue",
        "_release_admit",
        "_running",
        "_spooled",
        "_task",
    )

    _aborted: int
    _ctx: DaemonContext
    _embed_gate: asyncio.Semaphore
    _last_active: float
    _queue: asyncio.Queue[_Queued | None]
    _release_admit: Callable[[], None]
    _running: bool
    _spooled: int
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
        self._aborted = self._spooled = 0
        self._last_active = monotonic()
        # Start the resident consumer now; try_submit only ever runs inside a
        # request handler, so a running loop is guaranteed at creation.
        self._task = asyncio.create_task(self._run())
        return self

    def enqueue(self, job: IngestUnit, state: TaskState) -> None:
        """Append a job (with its task state) to this FIFO queue (never blocks)."""
        JobLifecycle(state).enqueue()
        self._queue.put_nowait(_Queued(job, state))

    def stop(self) -> None:
        """Enqueue the drain sentinel so the worker returns after its backlog."""
        self._queue.put_nowait(None)

    async def wait(self) -> None:
        """Await the worker's natural completion (after it drains to the sentinel)."""
        await self._task

    def is_alive(self) -> bool:
        """Return whether the worker's consumer task is still running."""
        return not self._task.done()

    def is_reapable(self) -> bool:
        """Return whether the worker holds no work and runs no job (safe to drop)."""
        return not self._running and self._queue.empty()

    def idle_seconds(self, now: float) -> float:
        """Return seconds since the worker last finished a job (or was created)."""
        return now - self._last_active

    def shutdown(self) -> asyncio.Task[None]:
        """Cancel an idle worker's task and return it for the caller to retain.

        The caller guarantees an empty queue.  Returning the cancelled task lets
        a synchronous reaper hold a strong reference (and a done-callback) until
        the cancellation completes, so the pending task is never garbage-collected
        mid-flight — asyncio keeps only a weak reference of its own.
        """
        self._task.cancel()
        return self._task

    def abort(self) -> asyncio.Task[None]:
        """Cancel the worker, fail every still-queued job, and return the task.

        Cancelling the task aborts the in-flight job, which ``_run`` spools as it
        unwinds (it still holds that job; by here it is already gone from the
        queue).  The jobs still waiting never ran, so this marks them ``failed``
        and frees their admit slots.  A job with a durable client artifact (a
        capture's transcript ``.md``) is recoverable via ``quarry backfill``; one
        without (``remember``/``ingest``) is spooled so an admitted job — queued
        OR in flight — is never *silently* dropped.  The cancelled task is
        returned so a sync caller can retain it the way ``aclose`` awaits it.
        """
        self._task.cancel()
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return self._task
            if item is not None:
                self._fail(item)
                self._release_admit()

    @property
    def aborted(self) -> int:
        """Return how many jobs this worker failed instead of running."""
        return self._aborted

    @property
    def spooled(self) -> int:
        """Return how many of those were spooled for recovery."""
        return self._spooled

    def _fail(self, item: _Queued) -> None:
        """Fail *item* with the truthful reason, spooling it first if it needs it."""
        item.state.status = "failed"
        item.state.error = AbortReason.for_settings(self._ctx.settings).of(item.job)
        self._aborted += 1
        self._spooled += "spooled for recovery" in item.state.error

    async def _run(self) -> None:
        """Drain jobs FIFO, one at a time, each under the shared embed gate."""
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            self._running = True
            try:
                # No coroutine-level timeout: job.run offloads the ingest to a
                # non-cancellable threadpool thread, so asyncio.wait_for could
                # not interrupt a hang — it would await the thread anyway and
                # hold the gate for the whole hang.  The one genuinely unbounded
                # wait is the web fetch, bounded by its own total deadline + size
                # cap (web_fetch.py); embed + LanceDB write are finite.
                await JobLifecycle(item.state).run(
                    item.job, self._ctx, self._embed_gate
                )
            except asyncio.CancelledError:
                # The drain-timeout abort cancels this task through the await
                # chain, so the in-flight job is aborted HERE — not in abort(),
                # which by now sees it already gone from the queue.  Spool it (a
                # capture returns None; its .md survives) so an admitted
                # remember/ingest is never silently lost, then let the
                # cancellation propagate.  This also covers a job cancelled while
                # still awaiting the embed gate (its TaskTerminal never ran).
                self._fail(item)
                raise
            finally:
                self._running = False
                self._last_active = monotonic()
                self._release_admit()
                self._queue.task_done()
