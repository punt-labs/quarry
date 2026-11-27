"""Turn watch batches and scans into IngestUnits on the DES-042 queue.

:class:`~quarry.daemon.watch_loop.WatchLoop` owns lifecycle and event
marshaling; this module owns the producer half: building
per-file / delete / bulk-scan / finalize jobs, submitting them on the
per-``(database, collection)`` queue, and re-arming a shed (503) live submit
through the debouncer with capped exponential backoff — a full queue is
transient and the file on disk is durable, so a change is delayed, never
dropped.  A shed *scan* or a failed job is recovered by the loop's periodic
disk-vs-registry reconcile, so no per-scan bookkeeping lives here.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.collection_finalize_job import CollectionFinalizeJob
from quarry.daemon.delta_submit import DeltaSubmission
from quarry.daemon.finalize_pacing import FinalizeThrottle
from quarry.daemon.finalize_retry import FinalizeRetry
from quarry.daemon.index_jobs import CollectionSyncJob
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.route_key import RouteKey
from quarry.daemon.scan_sweep import ScanSweep
from quarry.daemon.shed_rearm import ShedEventRearmer

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.debounce import DebouncedDispatcher, FlushBatch
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.task_state import TaskState
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.db import Database

logger = logging.getLogger(__name__)

# The routing key for finalize work on a database with no registered directory.
_MAINTENANCE_COLLECTION = "__maintenance__"


@final
class WatchSubmitter:
    """Submit watch-derived IngestUnits to the queue, re-arming shed live events.

    Batch and scan paths use separate collaborators for finalize throttling,
    scan sweeping, and re-arming shed events.
    """

    __slots__ = ("_ctx", "_deltas", "_rearm", "_retries", "_roster", "_throttle")

    _ctx: DaemonContext
    _deltas: DeltaSubmission
    _roster: WatchRoster
    _rearm: ShedEventRearmer
    _retries: FinalizeRetry
    _throttle: FinalizeThrottle

    def __new__(
        cls, ctx: DaemonContext, roster: WatchRoster, loop: asyncio.AbstractEventLoop
    ) -> Self:
        """Bind watch submissions to the roster, retry timers, and finalize throttle."""
        self = super().__new__(cls)
        self._ctx = ctx
        self._deltas = DeltaSubmission(ctx)
        self._roster = roster
        self._rearm = ShedEventRearmer(loop)
        self._retries = FinalizeRetry(loop)
        self._throttle = FinalizeThrottle(
            loop, ctx.settings.watch_optimize_min_interval_s
        )
        return self

    def cancel_pending(self) -> None:
        """Cancel every outstanding re-arm, trailing-finalize and retry timer."""
        self._rearm.cancel_pending()
        self._throttle.cancel_all()
        self._retries.cancel_all()

    def bind(self, dispatcher: DebouncedDispatcher) -> None:
        """Wire the debouncer used to re-arm shed events (sink is created first)."""
        self._rearm.bind(dispatcher)

    def forget(self, key: RouteKey) -> None:
        """Drop *key*'s backoff state (deregister/stop-watching)."""
        self._rearm.reset(key)

    def on_batch(self, batch: FlushBatch) -> None:
        """Dispatcher sink: turn one quiescent batch into queue submissions."""
        root = self._roster.resolved_root(batch.key)
        if root is None:
            return  # deregistered while the batch was pending
        db = self._roster.database_of(batch.key.database)
        settings = self._roster.settings_of(batch.key.database)
        if batch.bulk:
            # Index the burst now, but THROTTLE the heavy finalize. A sustained
            # bulk-churn workflow (branch switch, large rebase) trips the bulk
            # threshold every debounce window; an immediate finalize each time
            # would storm optimize+FTS and defeat the CPU envelope. Coalesce it
            # per database exactly like the delta path below.
            self.submit_scan_job(batch.key, root)
            self._request_finalize(batch.key, db, settings)
            return
        outcome = self._deltas.submit(batch, db, settings, root)
        if outcome.shed:
            self._rearm.defer(batch.key, list(outcome.shed))
        else:
            self._rearm.reset(batch.key)  # batch cleared — reset backoff
        if outcome.admitted:
            self._request_finalize(batch.key, db, settings)

    def sweep(self) -> ScanSweep:
        """Return a sweep that rescans collections, finalizing each database once.

        Every multi-collection rescan goes through this rather than calling
        :meth:`submit_scan` in a loop: the finalize is table-wide, so one per
        collection repeats identical work per registered collection.
        """
        return ScanSweep(self)

    def submit_scan(self, key: RouteKey, root: Path) -> list[TaskState]:
        """Submit a bulk scan + an IMMEDIATE finalize for ONE collection.

        The single-collection convenience over :meth:`sweep` — a newly watched
        registration, which by definition touches one collection.  A caller with
        several collections must use the sweep instead.
        """
        sweep = self.sweep()
        sweep.add(key, root)
        return sweep.finish()

    def submit_scan_job(self, key: RouteKey, root: Path) -> TaskState:
        """Submit the bulk re-scan/index job for *key*; return its task state."""
        db = self._roster.database_of(key.database)
        settings = self._roster.settings_of(key.database)
        scan = CollectionSyncJob(db, settings, key.collection, root)
        return self._submit_tracked(key, scan, "sync")

    def submit_finalize_job(self, key: RouteKey) -> TaskState:
        """Submit an IMMEDIATE finalize routed under *key*; return its task state.

        Immediate — not throttled — because every sweep is one-shot or the
        backstop: the initial watch scan, an explicit ``quarry sync`` whose
        umbrella awaits the finalize task, and the periodic reconcile (whose
        finalize is submitted immediately and never coalesced away, though it rewrites
        the table only if the table changed).
        The high-frequency bulk-churn path (``on_batch``) throttles its finalize
        instead.  A scan shed by a full queue is recovered by the reconcile.
        """
        db = self._roster.database_of(key.database)
        settings = self._roster.settings_of(key.database)
        job = CollectionFinalizeJob(
            db,
            settings,
            key.collection,
            partial(self.mark_finalized, key.database),
            partial(self.retry_finalize, key),
        )
        return self._submit_finalize_job(key, job)

    def mark_finalized(self, database: str) -> None:
        """Register an out-of-band finalize that is about to REWRITE *database*.

        Stamps the interval clock, so the throttle's next in-window request
        coalesces behind the rewrite that is running; a trailing finalize already
        armed is left alone, because a write queued behind this finalize may have
        armed it.  The finalize job calls this only when it decides to rewrite the
        table: a finalize that skips, or one the queue shed, never ran and must
        not stamp.
        """
        self._throttle.mark_admitted(database)

    def retry_database(self, database: str, delay_s: float | None) -> None:
        """Arm a finalize retry for *database*, which need not hold any directory.

        The retry rides a registered collection's FIFO when there is one, else
        the reserved maintenance collection's.
        """
        if delay_s is None:
            return
        self._roster.ensure_database(database)
        registered = self._roster.registrations(database)
        collection = registered[0][0] if registered else _MAINTENANCE_COLLECTION
        self.retry_finalize(RouteKey(database, collection), delay_s)

    def retry_finalize(self, key: RouteKey, delay_s: float) -> None:
        """Arm a retry for a finalize that left *key*'s database dirty."""
        self._retries.arm(key.database, delay_s, partial(self._retry_request, key))

    def _retry_request(self, key: RouteKey) -> None:
        """Request the retry through the throttle, which may keep another closure.

        The throttle arms one trailing timer per database and keeps whichever
        closure armed it, so a retry can run as a write's closure.  Re-arming a
        shed finalize therefore lives in the submit every finalize goes through,
        not in this closure.
        """
        try:
            db = self._roster.database_of(key.database)
            settings = self._roster.settings_of(key.database)
        except KeyError:
            logger.debug("retry dropped: database %s left the roster", key.database)
            return
        self._throttle.request(
            key.database,
            lambda: self._submit_finalize(key, db, settings).status != "failed",
        )

    def _request_finalize(
        self, key: RouteKey, db: Database, settings: Settings
    ) -> None:
        """Rate-limit the heavy finalize to one optimize+FTS rebuild per interval.

        Sustained churn coalesces per database, not per batch.  The finalize is
        table-wide, so running under this batch's (possibly later deregistered)
        collection key is harmless — it is only the routing key.
        """
        # The throttle stamps its interval clock only on admission, so the thunk
        # must distinguish a shed finalize (queue full) from one that ran.
        self._throttle.request(
            key.database,
            lambda: self._submit_finalize(key, db, settings).status != "failed",
        )

    def _submit_finalize(
        self, key: RouteKey, db: Database, settings: Settings
    ) -> TaskState:
        """Submit the coalesced FTS-rebuild finalize for *key*."""
        job = CollectionFinalizeJob(
            db,
            settings,
            key.collection,
            on_unsettled=partial(self.retry_finalize, key),
        )
        return self._submit_finalize_job(key, job)

    def _submit_finalize_job(self, key: RouteKey, job: IngestUnit) -> TaskState:
        """Submit a finalize job; if the queue sheds it, arm the database's retry.

        Every finalize submit passes through here, so whichever request produced
        it (a sweep, a write, a delta or bulk batch, a trailing timer, a retry), a
        shed finalize is retried after ``SHED_RETRY_S`` and is never lost.
        """
        state = self._submit_tracked(key, job, "sync")
        if state.status == "failed":
            self._retries.arm_shed(key.database, partial(self._retry_request, key))
        return state

    def _submit_tracked(self, key: RouteKey, job: IngestUnit, kind: str) -> TaskState:
        """Submit *job*, returning its task state (failed if the queue shed it)."""
        state = self._ctx.tasks.begin(kind)
        if not self._ctx.ingest_queue.try_submit(key, job, state):
            JobLifecycle(state).settle_failed("ingest queue full")
        return state
