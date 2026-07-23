"""Daemon-owned serialized ingest queue: one FIFO writer per collection.

Captures, remembers, and URL ingests arrive in bursts (several sessions compact
at once, a web-fetch lands beside a compaction, a backfill replays a directory).
Each is an independent LanceDB delete-then-insert against a shared ONNX embed
session.  Unbounded, a burst oversubscribes the cores and interleaves two
same-document overwrites so both chunk sets survive (DES-042 §2.1).

This module supplies the missing top layer above DES-034's op-level ``_write_lock``
and single-consumer ``ProgressiveIndexer``: exactly one in-flight
``progressive_insert`` caller per collection (a :class:`CollectionWorker`), and
at most ``EMBED_CONCURRENCY_CEILING`` embed jobs anywhere (a shared semaphore).
Admission is a non-blocking bounded gate — a full queue is a 503, never a blocked
hook (I-NOBLOCK).  The route key is client-controlled, so the resident worker map
is itself bounded: idle workers are reaped and the map is capped.  The routing
key is a :class:`RouteKey` — ``(database, collection)`` — so the always-on watch
loop (DES-045) can feed jobs for every database in the roster onto the same queue
without two databases' same-named collections ever sharing a worker.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.collection_worker import CollectionWorker

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.route_key import RouteKey
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

    __slots__ = (
        "_closing",
        "_ctx",
        "_depth",
        "_embed_gate",
        "_max_depth",
        "_max_workers",
        "_reaped",
        "_worker_idle_s",
        "_workers",
    )

    _ctx: DaemonContext
    _workers: dict[RouteKey, CollectionWorker]
    _embed_gate: asyncio.Semaphore
    _depth: int
    _max_depth: int
    _max_workers: int
    _worker_idle_s: float
    _closing: bool
    # Cancelled worker tasks retained until their cancellation completes: asyncio
    # keeps only a weak reference, so a reaped worker's still-pending task would
    # be garbage-collected mid-flight ("Task was destroyed but it is pending")
    # without a strong reference held here until the done-callback discards it.
    _reaped: set[asyncio.Task[None]]

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._workers = {}
        self._embed_gate = asyncio.Semaphore(cls._embed_concurrency(ctx))
        self._depth = 0
        self._max_depth = ctx.settings.ingest_queue_depth
        self._max_workers = ctx.settings.ingest_max_workers
        self._worker_idle_s = ctx.settings.ingest_worker_idle_s
        self._closing = False
        self._reaped = set()
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

    def try_submit(self, key: RouteKey, job: IngestUnit, state: TaskState) -> bool:
        """Admit *job* onto *key*'s FIFO worker; ``False`` if it cannot.

        Non-blocking: reaps stale workers, takes an admission slot without
        waiting, marks the task ``queued``, lazily starts the route key's
        worker, and hands off.  Returns ``False`` (the caller 503s) when the
        depth bound is hit or the worker cap is full of busy route keys — never
        awaits I/O, so the hook's 202 stays immediate (I-NOBLOCK).
        """
        if self._closing or self._depth >= self._max_depth:
            return False
        worker = self._worker_for(key)
        if worker is None:
            return False
        self._depth += 1
        state.status = "queued"
        worker.enqueue(job, state)
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
            self._retain(worker.abort())

    def _worker_for(self, key: RouteKey) -> CollectionWorker | None:
        """Return *key*'s worker, reaping idle ones and honoring the cap.

        ``None`` means the worker cap is full of busy route keys and the caller
        must shed load (503).  Reaping runs synchronously here, so no submit or
        worker can interleave between the idle check and the removal.
        """
        self._reap_idle(exclude=key)
        worker = self._workers.get(key)
        if worker is not None:
            return worker
        if len(self._workers) >= self._max_workers and not self._evict_reapable():
            return None
        worker = CollectionWorker(self._ctx, self._embed_gate, self._release_admit)
        self._workers[key] = worker
        return worker

    def _reap_idle(self, *, exclude: RouteKey) -> None:
        """Drop workers idle past the reap interval so client keys can't accrue.

        Synchronous: the empty/idle check and the removal cannot be split by a
        racing enqueue, so a reaped worker never strands a just-submitted job.
        A reaped route key is recreated lazily on its next submit and is, again,
        the sole writer for that ``(database, collection)`` table.
        """
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

    def _release_admit(self) -> None:
        """Free one admission slot when a job leaves the queue."""
        self._depth -= 1

    def _retain(self, task: asyncio.Task[None]) -> None:
        """Hold a cancelled worker task until its cancellation runs out.

        asyncio keeps only a weak reference to a task, so a reaped or aborted
        worker's still-pending task would be garbage-collected mid-cancel.
        Retaining it here — dropped by the done-callback once it finishes — keeps
        it alive to complete its cancellation, the synchronous counterpart of
        ``aclose`` awaiting ``worker.wait()`` under ``suppress(CancelledError)``.
        """
        self._reaped.add(task)
        task.add_done_callback(self._reaped.discard)
