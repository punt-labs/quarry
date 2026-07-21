"""Daemon-owned serialized ingest queue: one FIFO writer per collection.

Captures, remembers, and URL ingests arrive in bursts (several sessions compact
at once, a web-fetch lands beside a compaction, a backfill replays a directory).
Each is an independent LanceDB delete-then-insert against a shared ONNX embed
session.  Unbounded, a burst oversubscribes the cores and interleaves two
same-document overwrites so both chunk sets survive (DES-042 §2.1).

This module supplies the missing top layer above DES-034's op-level ``_write_lock``
and single-consumer ``ProgressiveIndexer``: exactly one in-flight
``progressive_insert`` caller per collection (a per-collection FIFO worker), and
at most ``EMBED_CONCURRENCY_CEILING`` embed jobs anywhere (a shared semaphore).
Admission is a non-blocking bounded gate — a full queue is a 503, never a blocked
hook (I-NOBLOCK).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)

# Hard ceiling on how many embed jobs run at once, across every collection.
# DES-032/DES-042: >1 buys no matmul parallelism (capture jobs share one ingest
# ONNX session and serialize on its ORT mutex regardless) and re-adds the
# >2-thread arena contention DES-032 tuned the per-job thread caps to avoid.
# One line widens the ceiling for an operator who accepts that trade; the
# ``ingest_embed_concurrency`` setting is clamped to it so an env override cannot
# exceed what this constant permits.
EMBED_CONCURRENCY_CEILING = 1


@final
@dataclass(frozen=True, slots=True)
class _Queued:
    """One admitted job paired with the task state it reports through."""

    job: IngestUnit
    state: TaskState


@final
class CollectionWorker:
    """One collection's FIFO worker: drains its queue under the shared embed gate.

    The worker *is* the per-collection serialization — a single consumer means
    only one ``progressive_insert`` caller for this collection is ever in flight,
    which is the single-writer precondition DES-034's ingest primitives assume.
    ``None`` on the queue is the drain sentinel; it never carries an admit slot,
    so it is not counted against the queue's admission bound.
    """

    __slots__ = ("_ctx", "_embed_gate", "_queue", "_release_admit", "_task")

    _ctx: DaemonContext
    _embed_gate: asyncio.Semaphore
    _queue: asyncio.Queue[_Queued | None]
    _release_admit: Callable[[], None]
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
        # Start the resident consumer now; try_submit only ever runs inside a
        # request handler, so a running loop is guaranteed at creation.
        self._task = asyncio.create_task(self._run())
        return self

    def enqueue(self, item: _Queued) -> None:
        """Append *item* to this collection's FIFO queue (never blocks)."""
        self._queue.put_nowait(item)

    def stop(self) -> None:
        """Enqueue the drain sentinel so the worker returns after its backlog."""
        self._queue.put_nowait(None)

    async def wait(self) -> None:
        """Await the worker's natural completion (after it drains to the sentinel)."""
        await self._task

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
        """Drain jobs FIFO, one at a time, each inside the shared embed gate."""
        while True:
            item = await self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            try:
                item.state.status = "running"  # queued -> running on dequeue
                async with self._embed_gate:  # global embed-concurrency bound
                    await item.job.run(self._ctx, item.state)  # records terminal
            finally:
                self._release_admit()
                self._queue.task_done()


@final
class IngestQueue:
    """Daemon-owned queue: one FIFO writer per collection, bounded embed globally.

    Two orthogonal bounds: a per-collection worker (a correctness bound — one
    writer per LanceDB table) and a shared embed semaphore (a performance bound —
    at most ``EMBED_CONCURRENCY_CEILING`` embed jobs anywhere).  A third bound,
    the admission counter, caps how many jobs may be admitted (in-flight +
    waiting) before the queue sheds load with a 503.
    """

    __slots__ = ("_closing", "_ctx", "_depth", "_embed_gate", "_max_depth", "_workers")

    _ctx: DaemonContext
    _workers: dict[str, CollectionWorker]
    _embed_gate: asyncio.Semaphore
    _depth: int
    _max_depth: int
    _closing: bool

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._workers = {}
        self._embed_gate = asyncio.Semaphore(cls._embed_concurrency(ctx))
        self._depth = 0
        self._max_depth = ctx.settings.ingest_queue_depth
        self._closing = False
        return self

    @staticmethod
    def _embed_concurrency(ctx: DaemonContext) -> int:
        """Clamp the configured embed concurrency to the hard ceiling."""
        configured = ctx.settings.ingest_embed_concurrency
        if configured > EMBED_CONCURRENCY_CEILING:
            logger.warning(
                "ingest_embed_concurrency=%d exceeds the hard ceiling %d; "
                "clamping (see DES-032)",
                configured,
                EMBED_CONCURRENCY_CEILING,
            )
        return min(configured, EMBED_CONCURRENCY_CEILING)

    def try_submit(self, collection: str, job: IngestUnit, state: TaskState) -> bool:
        """Admit *job* onto *collection*'s FIFO worker; ``False`` if the queue is full.

        Non-blocking: takes an admission slot without waiting, marks the task
        ``queued``, lazily starts the collection's worker, and hands off.  The
        caller returns 202 on ``True``, 503 on ``False`` — never awaits I/O, so
        the hook's 202 stays immediate (I-NOBLOCK).
        """
        if self._closing or self._depth >= self._max_depth:
            return False
        self._depth += 1
        state.status = "queued"
        self._worker_for(collection).enqueue(_Queued(job, state))
        return True

    async def aclose(self, *, drain_timeout: float) -> None:
        """Stop admitting, drain in-flight + queued jobs, then cancel stragglers.

        Every worker gets a drain sentinel and is awaited under *drain_timeout*
        so a clean shutdown flushes queued captures.  On timeout, the remaining
        workers are aborted — their in-flight and queued jobs record ``failed``
        (recoverable via backfill) — so shutdown never hangs.
        """
        self._closing = True
        workers = list(self._workers.values())
        for worker in workers:
            worker.stop()
        try:
            await asyncio.wait_for(
                asyncio.gather(*(worker.wait() for worker in workers)),
                timeout=drain_timeout,
            )
        except TimeoutError:
            for worker in workers:
                worker.abort()
            for worker in workers:
                with suppress(asyncio.CancelledError):
                    await worker.wait()

    def cancel_workers(self) -> None:
        """Synchronously stop every worker (test-harness teardown, no await).

        The async counterpart of ``aclose`` for callers that cannot await — it
        aborts each worker so no queued job runs into a later test's mock window.
        """
        self._closing = True
        for worker in self._workers.values():
            worker.abort()

    def _worker_for(self, collection: str) -> CollectionWorker:
        """Return *collection*'s worker, starting it on first use."""
        worker = self._workers.get(collection)
        if worker is None:
            worker = CollectionWorker(self._ctx, self._embed_gate, self._release_admit)
            self._workers[collection] = worker
        return worker

    def _release_admit(self) -> None:
        """Free one admission slot when a job leaves the queue."""
        self._depth -= 1
