"""One collection's resident FIFO ingest worker and its queued-job message.

A :class:`CollectionWorker` is the per-collection serialization the queue is
built on: a single consumer coroutine draining one ``asyncio.Queue`` means only
one ``progressive_insert`` caller per LanceDB collection is ever in flight —
the single-writer precondition DES-034's ingest primitives assume.  Each unit
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

from quarry.daemon.job_spool import JobSpool

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
        spool = JobSpool.for_settings(self._ctx.settings)
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return self._task
            if item is not None:
                self._fail_aborted(item, spool)

    def _fail_aborted(self, item: _Queued, spool: JobSpool) -> None:
        """Fail a still-queued job, spooling it when it has no durable copy."""
        item.state.status = "failed"
        item.state.error = self._abort_reason(item.job, spool)
        self._release_admit()

    @staticmethod
    def _abort_reason(job: IngestUnit, spool: JobSpool) -> str:
        """Return the truthful terminal error for a drain-aborted job.

        A job with a durable client copy (a capture's transcript ``.md``) is
        recoverable via ``quarry backfill``.  One without is spooled — but ONLY a
        spool write that actually succeeded may claim recoverability, so a failed
        write (already logged) records the loss truthfully rather than a false
        "spooled" claim that would hide the exact silent-loss this path prevents.
        """
        record = job.spool_record()
        if record is None:
            return "daemon shut down before ingest completed"
        if spool.write(record):
            return "daemon shut down before ingest; spooled for recovery"
        return (
            "daemon shut down before ingest; spool write failed, content not recovered"
        )

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
                # wait is the web fetch, bounded by its own total deadline + size
                # cap (web_fetch.py); embed + LanceDB write are finite.
                async with self._embed_gate:  # global embed-concurrency bound
                    await item.job.run(self._ctx, item.state)  # records terminal
            except asyncio.CancelledError:
                # The drain-timeout abort cancels this task through the await
                # chain, so the in-flight job is aborted HERE — not in abort(),
                # which by now sees it already gone from the queue.  Spool it (a
                # capture returns None; its .md survives) so an admitted
                # remember/ingest is never silently lost, then let the
                # cancellation propagate.  This also covers a job cancelled while
                # still awaiting the embed gate (its task_terminal never ran).
                self._mark_aborted(item)
                raise
            finally:
                self._running = False
                self._last_active = monotonic()
                self._release_admit()
                self._queue.task_done()

    def _mark_aborted(self, item: _Queued) -> None:
        """Record a drain-cancelled in-flight job as failed, spooling it first."""
        item.state.status = "failed"
        item.state.error = self._abort_reason(
            item.job, JobSpool.for_settings(self._ctx.settings)
        )
