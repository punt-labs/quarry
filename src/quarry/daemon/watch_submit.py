"""Turn watch batches and scans into IngestUnits on the DES-042 queue.

Extracted from :class:`~quarry.daemon.watch_loop.WatchLoop` so the loop owns
lifecycle + event marshaling while this owns the producer half: building
per-file / delete / bulk-scan / finalize jobs, submitting them on the
per-``(database, collection)`` queue, and re-arming a shed (503) submit through
the debouncer with capped exponential backoff — a full queue is transient and
the file on disk is durable, so a change is delayed, never dropped.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.finalize_job import CollectionFinalizeJob
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.route_key import RouteKey

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

# 503 backoff: a shed submit re-arms after this delay, doubling to the cap.
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 30.0


@final
class WatchSubmitter:
    """Submit watch-derived IngestUnits to the queue, re-arming shed ones."""

    __slots__ = ("_backoff", "_ctx", "_dispatcher", "_loop", "_roster")

    _ctx: DaemonContext
    _roster: WatchRoster
    _loop: asyncio.AbstractEventLoop
    _dispatcher: DebouncedDispatcher | None
    _backoff: dict[RouteKey, float]

    def __new__(
        cls, ctx: DaemonContext, roster: WatchRoster, loop: asyncio.AbstractEventLoop
    ) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._roster = roster
        self._loop = loop
        self._dispatcher = None
        self._backoff = {}
        return self

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
        """Submit a bulk scan then its coalesced finalize; return their task states."""
        db = self._roster.database_of(key.database)
        settings = self._roster.settings_of(key.database)
        scan = CollectionSyncJob(db, settings, key.collection, root)
        return [
            self._submit_tracked(key, scan, "sync"),
            self._submit_finalize(key, db, settings),
        ]

    def _submit_deltas(
        self, batch: FlushBatch, db: Database, settings: Settings, root: Path
    ) -> list[FsEvent]:
        """Submit each per-file index/delete job; return the events the queue shed."""
        failed: list[FsEvent] = []
        for path in batch.modified:
            job = FileIndexJob(db, settings, batch.key.collection, root, path)
            if not self._submit(batch.key, job, "index"):
                failed.append(FsEvent(path, deleted=False))
        for path in batch.deleted:
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
        self._loop.call_later(delay, self._refeed, key, tuple(events))

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
