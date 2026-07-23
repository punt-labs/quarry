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

from quarry.daemon.finalize_job import CollectionFinalizeJob, CollectionPurgeJob
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.route_key import RouteKey
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS
from quarry.sync_discovery import FileDiscovery

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Sequence
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.debounce import DebouncedDispatcher, FlushBatch
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.db import Database

logger = logging.getLogger(__name__)

# 503 backoff: a shed live submit re-arms after this delay, doubling to the cap.
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 30.0


@final
class WatchSubmitter:
    """Submit watch-derived IngestUnits to the queue, re-arming shed live events."""

    __slots__ = (
        "_backoff",
        "_ctx",
        "_dispatcher",
        "_loop",
        "_pending_purges",
        "_roster",
        "_timers",
    )

    _ctx: DaemonContext
    _roster: WatchRoster
    _loop: asyncio.AbstractEventLoop
    _dispatcher: DebouncedDispatcher | None
    _backoff: dict[RouteKey, float]
    # Pending backoff re-arm timers, tracked so shutdown can cancel them.
    _timers: set[asyncio.TimerHandle]
    # Subsume-purges the queue rejected, retried on the next reconcile.
    _pending_purges: set[RouteKey]

    def __new__(
        cls, ctx: DaemonContext, roster: WatchRoster, loop: asyncio.AbstractEventLoop
    ) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._roster = roster
        self._loop = loop
        self._dispatcher = None
        self._backoff = {}
        self._timers = set()
        self._pending_purges = set()
        return self

    def defer_purge(self, key: RouteKey) -> None:
        """Queue a failed subsume-purge for reconcile-driven re-admission.

        A subsume-purge the saturated queue rejected leaves orphan chunks with no
        other backstop — reconcile-drop tears a gone collection's watch down but
        never purges its chunks — so the collection is retried until the queue
        admits the delete.
        """
        self._pending_purges.add(key)

    def discard_pending_purge(self, key: RouteKey) -> None:
        """Cancel a deferred purge because *key* is registered (and live) again.

        A same-name re-registration makes a collection live at a new root before
        the next reconcile; its earlier orphans are moot and purging would wipe
        the live parent's fresh chunks — so a re-watch supersedes the stale purge.
        """
        self._pending_purges.discard(key)

    def drain_pending_purges(self, live: set[RouteKey]) -> None:
        """Re-submit each deferred purge whose collection is no longer registered.

        A key that is *live* (in the on-disk roster) was re-registered after its
        purge was deferred; purging it now would destroy the live collection's
        chunks, so it is dropped WITHOUT submitting.  For a still-absent key,
        admission of the ``CollectionPurgeJob`` is the retry's success condition —
        a still-full queue keeps it for the next reconcile.
        """
        if not self._pending_purges:
            return
        still: set[RouteKey] = set()
        for key in self._pending_purges:
            if key in live:
                continue  # re-registered — its orphans are moot; never purge live
            task = self._ctx.tasks.begin("subsume-purge-retry")
            job = CollectionPurgeJob(self._ctx.database, key.collection)
            if not self._ctx.ingest_queue.try_submit(key, job, task):
                self._ctx.tasks.drop(task)
                still.add(key)
        self._pending_purges = still

    def cancel_pending(self) -> None:
        """Cancel every outstanding backoff re-arm timer (shutdown)."""
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()

    def bind(self, dispatcher: DebouncedDispatcher) -> None:
        """Wire the debouncer used to re-arm shed events (sink is created first)."""
        self._dispatcher = dispatcher

    def forget(self, key: RouteKey) -> None:
        """Drop *key*'s backoff state (deregister/stop-watching)."""
        self._backoff.pop(key, None)

    def on_batch(self, batch: FlushBatch) -> None:
        """Dispatcher sink: turn one quiescent batch into queue submissions."""
        root = self._roster.resolved_root(batch.key)
        if root is None:
            return  # deregistered while the batch was pending
        db = self._roster.database_of(batch.key.database)
        settings = self._roster.settings_of(batch.key.database)
        if batch.bulk:
            self.submit_scan(batch.key, root)
            return
        failed = self._submit_deltas(batch, db, settings, root)
        if failed:
            self._defer(batch.key, failed)
            return
        self._backoff.pop(batch.key, None)  # batch cleared — reset backoff
        self._submit_finalize(batch.key, db, settings)

    def submit_scan(self, key: RouteKey, root: Path) -> list[TaskState]:
        """Submit a bulk scan then its coalesced finalize; return their task states.

        A scan shed by a full queue is recovered by the periodic reconcile, which
        re-scans every registered collection, so no retry state is tracked here.
        """
        db = self._roster.database_of(key.database)
        settings = self._roster.settings_of(key.database)
        scan = CollectionSyncJob(db, settings, key.collection, root)
        return [
            self._submit_tracked(key, scan, "sync"),
            self._submit_finalize(key, db, settings),
        ]

    @staticmethod
    def summarize_scan(
        umbrella: TaskState, children: list[TaskState], *, timed_out: bool
    ) -> None:
        """Roll the child scans' per-file failures + errors up into *umbrella*.

        A ``CollectionSyncJob`` completes even when N files failed (it records
        ``failed``/``errors`` in its own state), so counting only child *status*
        would report silent success.  Aggregate both the shed-job count and the
        per-file failure count/errors, and fail the umbrella if either is nonzero.
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
            "collections": len(children) // 2,
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
            state.status = "failed"
            state.error = "ingest queue full"
        return state

    def _submit(self, key: RouteKey, job: IngestUnit, kind: str) -> bool:
        """Submit *job*; drop its task record and return False if the queue is full."""
        state = self._ctx.tasks.begin(kind)
        if self._ctx.ingest_queue.try_submit(key, job, state):
            return True
        self._ctx.tasks.drop(state)
        return False

    def _defer(self, key: RouteKey, events: list[FsEvent]) -> None:
        """Re-arm shed events after an exponential-backoff delay (never dropped)."""
        delay = self._backoff.get(key, _BACKOFF_BASE_S)
        self._backoff[key] = min(delay * 2, _BACKOFF_MAX_S)
        # Prune fired handles so the set stays bounded, then track the new one so
        # shutdown can cancel a still-pending re-arm (no stray timer post-stop).
        now = self._loop.time()
        self._timers = {timer for timer in self._timers if timer.when() > now}
        self._timers.add(self._loop.call_later(delay, self._refeed, key, tuple(events)))

    def _refeed(self, key: RouteKey, events: Sequence[FsEvent]) -> None:
        """Feed deferred events back through the debouncer for another attempt."""
        if self._dispatcher is None:
            return
        for event in events:
            self._dispatcher.feed(key, event)

    @staticmethod
    def _document_name(root: Path, path: Path) -> str | None:
        """Return *path*'s registry document name (relative to *root*), or None."""
        try:
            return str(path.relative_to(root))
        except ValueError:
            return None
