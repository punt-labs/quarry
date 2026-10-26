"""Daemon-owned serialized ingest queue: one FIFO writer per collection.

Captures, remembers, and URL ingests arrive in bursts (several sessions compact
at once, a web-fetch lands beside a compaction, a backfill replays a directory).
Each is an independent LanceDB delete-then-insert against a shared ONNX embed
session.  Unbounded, a burst oversubscribes the cores and interleaves two
same-document overwrites so both chunk sets survive.

This module supplies the missing top layer above the op-level ``_write_lock``
and single-consumer ``ProgressiveIndexer``: exactly one in-flight
``DocumentStreamer.insert`` caller per collection (a :class:`CollectionWorker`), and
at most ``EMBED_CONCURRENCY_CEILING`` embed jobs anywhere (a shared semaphore).
Admission is a non-blocking bounded gate — a full queue is a 503, never a blocked
hook (I-NOBLOCK).  The route key is client-controlled, so the resident worker map
is itself bounded: idle workers are reaped and the map is capped.  The routing
key is a :class:`RouteKey` — ``(database, collection)`` — so the always-on watch
loop can feed jobs for every database in the roster onto the same queue
without two databases' same-named collections ever sharing a worker.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.daemon.loop_close import LoopClose
from quarry.daemon.queue_abort import QueueAbort
from quarry.daemon.queue_bounds import QueueGate, WorkerPool
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    from quarry.daemon.collection_worker import CollectionWorker
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.route_key import RouteKey
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)

# Hard ceiling on how many embed jobs run at once, across every collection.
# >1 buys no matmul parallelism (capture jobs share one ingest
# ONNX session and serialize on its ORT mutex regardless) and re-adds the
# >2-thread arena contention the per-job thread caps were tuned to avoid.
# One line widens the ceiling for an operator who accepts that trade; the
# ``ingest_embed_concurrency`` setting is clamped to it so an env override cannot
# exceed what this constant permits.
EMBED_CONCURRENCY_CEILING = 1


@final
class IngestQueue:
    """Daemon-owned queue: one FIFO writer per route key, bounded embed globally.

    Two orthogonal bounds: a per-route-key worker (a correctness bound — one
    writer per LanceDB table, keyed ``(database, collection)``) and a shared
    embed semaphore (a performance bound — at most ``EMBED_CONCURRENCY_CEILING``
    embed jobs anywhere).  An admission counter caps how many jobs may be
    admitted (in-flight + waiting) before the queue sheds load with a 503, and —
    because the route key is client-controlled — the resident worker map is
    itself bounded: idle workers are reaped and the map is capped at
    ``ingest_max_workers``.
    """

    # The most an abort waits for its workers to unwind: cancelling a worker whose
    # job sits in a non-cancellable thread must not hold shutdown for good.
    ABORT_WAIT_S: ClassVar[float] = ShutdownPolicy.ABORT_WAIT_S

    __slots__ = ("_gate", "_pool")

    _gate: QueueGate
    _pool: WorkerPool

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        embed_gate = asyncio.Semaphore(cls._embed_concurrency(ctx))
        self._gate = QueueGate(ctx.settings.ingest_queue_depth)
        self._pool = WorkerPool(ctx, embed_gate, self._gate.release)
        return self

    @staticmethod
    def _embed_concurrency(ctx: DaemonContext) -> int:
        """Clamp the configured embed concurrency to the hard ceiling."""
        configured = ctx.settings.ingest_embed_concurrency
        if configured > EMBED_CONCURRENCY_CEILING:
            logger.warning(
                "ingest_embed_concurrency=%d exceeds the hard ceiling %d; clamping",
                configured,
                EMBED_CONCURRENCY_CEILING,
            )
        return min(configured, EMBED_CONCURRENCY_CEILING)

    @property
    def gate(self) -> QueueGate:
        """Return the admission gate: its depth, its close, and who waits for room."""
        return self._gate

    @property
    def pool(self) -> WorkerPool:
        """Return the resident per-collection workers."""
        return self._pool

    def try_submit(self, key: RouteKey, job: IngestUnit, state: TaskState) -> bool:
        """Admit *job* onto *key*'s FIFO worker; ``False`` if it cannot.

        Non-blocking: reaps stale workers, takes an admission slot without
        waiting, lazily starts the route key's worker, and hands off (the worker
        marks the task ``queued``).  Returns ``False`` (the caller 503s) when the
        depth bound is hit or the worker cap is full of busy route keys — never
        awaits I/O, so the hook's 202 stays immediate (I-NOBLOCK).
        """
        if not self._gate.try_take():
            return False
        worker = self._pool.for_key(key)
        if worker is None:
            self._gate.release()
            return False
        worker.enqueue(job, state)
        return True

    async def aclose(self, *, drain_timeout: float) -> None:
        """Stop admitting, drain in-flight + queued jobs, then cancel stragglers.

        Every worker gets a drain sentinel and is awaited under *drain_timeout*
        so a clean shutdown flushes queued captures.  On timeout, the remaining
        workers are aborted — their in-flight and queued jobs record ``failed``
        (recoverable via backfill) — and awaited for :attr:`ABORT_WAIT_S` at most,
        so shutdown never hangs.  The timeout says how many jobs it failed and
        how many of those it spooled.  Then it uses what is left of that
        allowance to wait for the stores a cancelled worker left running and for
        the spool records they made unnecessary (:class:`LoopClose`), logs the
        tally as it then stands, and names every store still running when the
        loop is about to close: its spool record, if any, may then duplicate
        stored content.
        """
        self._gate.close()
        workers = self._pool.workers()
        for worker in workers:
            worker.stop()
        try:
            await asyncio.wait_for(
                asyncio.gather(*(worker.wait() for worker in workers)),
                timeout=drain_timeout,
            )
        except TimeoutError:
            await self._abort(workers, drain_timeout)

    async def _abort(self, workers: list[CollectionWorker], window_s: float) -> None:
        """Abort *workers* after a drain outran its window; say what that cost.

        The abort's wait and the drain that follows share one allowance,
        :attr:`ABORT_WAIT_S`, so a shutdown is no longer than it was: the drain
        gets what the abort left, and reports whatever it could not wait out.
        """
        started = asyncio.get_running_loop().time()
        await QueueAbort(workers, self.ABORT_WAIT_S).run(window_s)
        await LoopClose(workers).run(started + self.ABORT_WAIT_S)

    def cancel_workers(self) -> None:
        """Synchronously stop every worker (test-harness teardown, no await).

        The async counterpart of ``aclose`` for callers that cannot await — it
        aborts each worker so no queued job runs into a later test's mock window.
        """
        self._gate.close()
        self._pool.abort()
