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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.finalize_job import CollectionFinalizeJob
from quarry.daemon.finalize_throttle import FinalizeThrottle
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.route_key import RouteKey
from quarry.daemon.scan_sweep import ScanSweep
from quarry.daemon.shed_rearm import ShedEventRearmer
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS
from quarry.sync_discovery import FileDiscovery

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


@final
@dataclass(frozen=True, slots=True)
class _DeltaOutcome:
    """What one delta batch did: the jobs the queue admitted and the events it shed."""

    admitted: int
    shed: tuple[FsEvent, ...]


@final
class WatchSubmitter:
    """Submit watch-derived IngestUnits to the queue, re-arming shed live events.

    Batch and scan paths use separate collaborators for finalize throttling,
    scan sweeping, and re-arming shed events.
    """

    __slots__ = ("_ctx", "_rearm", "_roster", "_throttle")

    _ctx: DaemonContext
    _roster: WatchRoster
    _rearm: ShedEventRearmer
    _throttle: FinalizeThrottle

    def __new__(
        cls, ctx: DaemonContext, roster: WatchRoster, loop: asyncio.AbstractEventLoop
    ) -> Self:
        """Bind watch submissions to the roster, retry timers, and finalize throttle."""
        self = super().__new__(cls)
        self._ctx = ctx
        self._roster = roster
        self._rearm = ShedEventRearmer(loop)
        self._throttle = FinalizeThrottle(
            loop, ctx.settings.watch_optimize_min_interval_s
        )
        return self

    def cancel_pending(self) -> None:
        """Cancel every outstanding backoff re-arm and trailing-finalize timer."""
        self._rearm.cancel_pending()
        self._throttle.cancel_all()

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
        outcome = self._submit_deltas(batch, db, settings, root)
        if outcome.shed:
            self._rearm.defer(batch.key, list(outcome.shed))
            return
        self._rearm.reset(batch.key)  # batch cleared — reset backoff
        if not outcome.admitted:
            return  # every path was filtered out: the table did not change
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
        finalize is the FTS self-heal and must always run, never coalesce away).
        The high-frequency bulk-churn path (``on_batch``) throttles its finalize
        instead.  A scan shed by a full queue is recovered by the reconcile.
        """
        db = self._roster.database_of(key.database)
        settings = self._roster.settings_of(key.database)
        return self._submit_finalize(key, db, settings)

    def mark_finalized(self, database: str) -> None:
        """Register an out-of-band finalize that was ADMITTED for *database*.

        Cancels any trailing finalize already armed for it and resets the
        interval clock — otherwise a sweep's finalize and an armed trailing
        finalize would both run (a redundant double-compaction).  Callers
        register ONLY on admission: a shed finalize never ran, so it must not
        stamp and must leave a pending trailing timer to retry.
        """
        self._throttle.mark_admitted(database)

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

    def _submit_deltas(
        self, batch: FlushBatch, db: Database, settings: Settings, root: Path
    ) -> _DeltaOutcome:
        """Submit each per-file index/delete job; report what was admitted or shed.

        The authoritative filter runs here, post-debounce (once per distinct path
        per window, off the observer thread): ``is_indexable`` resolves the real
        path and rejects a symlink escaping the tree (security) plus applies the
        ignore rules BEFORE any content is read; a delete gets lexical
        ``is_deletable``.  A rejected path is dropped, not re-armed.
        """
        discovery = FileDiscovery(root)
        admitted = 0
        failed: list[FsEvent] = []
        for path in batch.modified:
            if not discovery.is_indexable(path, SUPPORTED_EXTENSIONS):
                continue
            job = FileIndexJob(db, settings, batch.key.collection, root, path)
            if self._submit(batch.key, job, "index"):
                admitted += 1
            else:
                failed.append(FsEvent(path, deleted=False))
        for path in batch.deleted:
            if not discovery.is_deletable(path, SUPPORTED_EXTENSIONS):
                continue
            name = self._document_name(root, path)
            if name is None:
                continue
            delete = DocumentDeleteJob(db, settings, batch.key.collection, (name,))
            if self._submit(batch.key, delete, "delete"):
                admitted += 1
            else:
                failed.append(FsEvent(path, deleted=True))
        return _DeltaOutcome(admitted, tuple(failed))

    def _submit_finalize(
        self, key: RouteKey, db: Database, settings: Settings
    ) -> TaskState:
        """Submit the coalesced FTS-rebuild finalize for *key*."""
        job = CollectionFinalizeJob(db, settings, key.collection)
        return self._submit_tracked(key, job, "sync")

    def _submit_tracked(self, key: RouteKey, job: IngestUnit, kind: str) -> TaskState:
        """Submit *job*, returning its task state (failed if the queue shed it)."""
        state = self._ctx.tasks.begin(kind)
        if not self._ctx.ingest_queue.try_submit(key, job, state):
            JobLifecycle(state).settle_failed("ingest queue full")
        return state

    def _submit(self, key: RouteKey, job: IngestUnit, kind: str) -> bool:
        """Submit *job*; drop its task record and return False if the queue is full."""
        state = self._ctx.tasks.begin(kind)
        if self._ctx.ingest_queue.try_submit(key, job, state):
            return True
        self._ctx.tasks.drop(state)
        return False

    @staticmethod
    def _document_name(root: Path, path: Path) -> str | None:
        """Return *path*'s registry document name (relative to *root*), or None."""
        try:
            return str(path.relative_to(root))
        except ValueError:
            return None
