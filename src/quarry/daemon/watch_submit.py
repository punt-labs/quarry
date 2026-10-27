"""Turn watch batches and scans into IngestUnits on the DES-042 queue.

Extracted from :class:`~quarry.daemon.watch_loop.WatchLoop` so the loop owns
lifecycle + event marshaling while this owns the producer half: building
per-file / delete / bulk-scan / finalize jobs, submitting them on the
per-``(database, collection)`` queue, and re-arming a shed (503) live submit
through the debouncer with capped exponential backoff — a full queue is
transient and the file on disk is durable, so a change is delayed, never
dropped.  A shed *scan* or a failed job is recovered by the loop's periodic
disk-vs-registry reconcile, so no per-scan bookkeeping lives here.
"""

from __future__ import annotations

import logging
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
    from quarry.daemon.tasks import TaskState
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.db import Database

logger = logging.getLogger(__name__)


@final
class WatchSubmitter:
    """Submit watch-derived IngestUnits to the queue, re-arming shed live events.

    Carries known cohesion debt: LCOM 0.78 against a 0.5 target, because the
    batch path and the scan path reach different collaborators.  Three classes
    have already moved out (the finalize throttle, the scan sweep, the shed
    re-arm), and the rule for whoever comes next is that the remainder gets
    split further rather than relaxed again.
    """

    __slots__ = ("_ctx", "_rearm", "_roster", "_throttle")

    _ctx: DaemonContext
    _roster: WatchRoster
    _rearm: ShedEventRearmer
    _throttle: FinalizeThrottle

    def __new__(
        cls, ctx: DaemonContext, roster: WatchRoster, loop: asyncio.AbstractEventLoop
    ) -> Self:
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
            self._throttle.request(
                batch.key.database,
                lambda: self._admitted_finalize(batch.key, db, settings),
            )
            return
        failed = self._submit_deltas(batch, db, settings, root)
        if failed:
            self._rearm.defer(batch.key, failed)
            return
        self._rearm.reset(batch.key)  # batch cleared — reset backoff
        # Rate-limit the heavy finalize: sustained churn coalesces into one
        # optimize+FTS-rebuild per interval per database, not one per batch. The
        # finalize is table-wide, so running under this batch's (possibly later
        # deregistered) collection key is harmless — it is only the routing key.
        self._throttle.request(
            batch.key.database,
            lambda: self._admitted_finalize(batch.key, db, settings),
        )

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

    def _admitted_finalize(
        self, key: RouteKey, db: Database, settings: Settings
    ) -> bool:
        """Submit the coalesced finalize; return True iff the queue admitted it.

        The throttle stamps its interval clock only on admission, so the return
        must distinguish a shed finalize (queue full) from one that ran.
        """
        return self._submit_finalize(key, db, settings).status != "failed"

    @staticmethod
    def summarize_scan(
        umbrella: TaskState,
        children: list[TaskState],
        collections: int,
        *,
        timed_out: bool,
    ) -> None:
        """Roll the child scans' per-file failures + errors up into *umbrella*.

        A ``CollectionSyncJob`` completes even when N files failed (it records
        ``failed``/``errors`` in its own state), so counting only child *status*
        would report silent success.  Aggregate both the shed-job count and the
        per-file failure count/errors, and fail the umbrella if either is nonzero.

        *collections* is reported by the sweep rather than derived from the child
        count: children are N scans plus one finalize per database, so no fixed
        arithmetic over ``children`` recovers it.
        """
        shed = sum(1 for child in children if child.status == "failed")
        file_failures = 0
        errors: list[str] = []
        for child in children:
            failed = child.results.get("failed", 0)
            if isinstance(failed, int):
                file_failures += failed
            child_errors = child.results.get("errors")
            if isinstance(child_errors, list):
                errors.extend(str(error) for error in child_errors)
        umbrella.results = {
            "collections": collections,
            "failed": file_failures,
            "shed": shed,
            "errors": errors,
        }
        if timed_out:
            umbrella.status = "failed"
            umbrella.error = "scan timed out before all jobs completed"
        elif shed or file_failures:
            umbrella.status = "failed"
            umbrella.error = f"{shed} scan job(s) shed, {file_failures} file(s) failed"
        else:
            umbrella.status = "completed"

    def _submit_deltas(
        self, batch: FlushBatch, db: Database, settings: Settings, root: Path
    ) -> list[FsEvent]:
        """Submit each per-file index/delete job; return the events the queue shed.

        The authoritative filter runs here, post-debounce (once per distinct path
        per window, off the observer thread): ``is_indexable`` resolves the real
        path and rejects a symlink escaping the tree (security) plus applies the
        ignore rules BEFORE any content is read; a delete gets lexical
        ``is_deletable``.  A rejected path is dropped, not re-armed.
        """
        discovery = FileDiscovery(root)
        failed: list[FsEvent] = []
        for path in batch.modified:
            if not discovery.is_indexable(path, SUPPORTED_EXTENSIONS):
                continue
            job = FileIndexJob(db, settings, batch.key.collection, root, path)
            if not self._submit(batch.key, job, "index"):
                failed.append(FsEvent(path, deleted=False))
        for path in batch.deleted:
            if not discovery.is_deletable(path, SUPPORTED_EXTENSIONS):
                continue
            name = self._document_name(root, path)
            if name is None:
                continue
            delete = DocumentDeleteJob(db, settings, batch.key.collection, (name,))
            if not self._submit(batch.key, delete, "delete"):
                failed.append(FsEvent(path, deleted=True))
        return failed

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
