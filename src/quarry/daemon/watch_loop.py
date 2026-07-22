"""The always-on filesystem watch loop: a producer onto the DES-042 queue.

One ``WatchLoop`` per daemon watches every registered directory across every
database in the roster (DES-045).  It is a *producer only*: a debounced fs-event
becomes a :class:`FileIndexJob`/:class:`DocumentDeleteJob` (small delta) or a
single :class:`CollectionSyncJob` (a burst above ``watch_bulk_threshold``), each
submitted to the existing per-``(database, collection)`` queue.  It writes no
LanceDB table itself and invents no second queue — it inherits the whole
serialization stack (DES-042 FIFO → DES-034 progressive commit → DES-026 WAL).

The FTS rebuild is coalesced: once a ``(database, collection)`` goes quiescent a
lone :class:`CollectionFinalizeJob` runs the index rebuild, so per-file indexing
never reopens the quarry-0dss descriptor leak.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.debounce import DebouncedDispatcher
from quarry.daemon.finalize_job import CollectionFinalizeJob
from quarry.daemon.fs_events import FsEvent
from quarry.daemon.fs_watchdog import WatchdogSource
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import task_terminal
from quarry.daemon.watch_roster import WatchRoster
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.debounce import FlushBatch
    from quarry.daemon.fs_events import FsEventSource
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState
    from quarry.db import Database

logger = logging.getLogger(__name__)

# 503 backoff: a shed submit re-arms after this delay, doubling to the cap, never
# dropping the change (a full queue is transient; the file on disk is durable).
_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 30.0
_TERMINAL = frozenset({"completed", "failed"})


@final
class WatchLoop:
    """Watch every roster database's registered trees and feed the ingest queue."""

    __slots__ = (
        "_backoff",
        "_ctx",
        "_dispatcher",
        "_loop",
        "_roster",
        "_source",
        "_started",
    )

    _ctx: DaemonContext
    _source: FsEventSource | None
    _roster: WatchRoster | None
    _dispatcher: DebouncedDispatcher | None
    _loop: asyncio.AbstractEventLoop | None
    _backoff: dict[RouteKey, float]
    _started: bool

    def __new__(
        cls, ctx: DaemonContext, *, source: FsEventSource | None = None
    ) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        # A test injects a synthetic source; production builds a watchdog observer
        # lazily in start() so importing the loop never starts a thread.
        self._source = source
        self._roster = None
        self._dispatcher = None
        self._loop = None
        self._backoff = {}
        self._started = False
        return self

    async def start(self) -> None:
        """Open roster connections, schedule every tree, and submit initial scans."""
        settings = self._ctx.settings
        if not settings.watch_enabled:
            logger.info("watch: disabled (watch_enabled=false)")
            return
        self._loop = asyncio.get_running_loop()
        if self._source is None:
            self._source = WatchdogSource(
                use_polling=settings.watch_use_polling,
                poll_interval_s=settings.watch_poll_interval_s,
            )
        self._roster = WatchRoster(
            self._source,
            active_db=self._ctx.database_name,
            active_database=self._ctx.database,
            base_settings=settings,
        )
        self._dispatcher = DebouncedDispatcher(
            self._loop,
            debounce_s=settings.watch_debounce_s,
            max_delay_s=settings.watch_max_delay_s,
            bulk_threshold=settings.watch_bulk_threshold,
            sink=self._on_batch,
        )
        self._started = True
        for name in self._roster.roster_names():
            self._start_database(name)

    async def stop(self) -> None:
        """Tear down the observer and drop pending state (before the queue drain)."""
        if not self._started:
            return
        self._started = False
        if self._dispatcher is not None:
            self._dispatcher.cancel_all()
        if self._roster is not None:
            self._roster.unwatch_all()
        if self._source is not None:
            # stop() joins the observer thread — off the loop so shutdown never blocks.
            await run_in_threadpool(self._source.stop)

    def start_watching(self, collection: str, resolved_root: Path) -> None:
        """Begin watching *collection* in the active database + submit its scan."""
        if not self._started:
            return
        self._begin_collection(self._ctx.database_name, collection, resolved_root)

    def stop_watching(self, collection: str) -> None:
        """Stop watching *collection* and drop its pending changes (before purge)."""
        if not self._started or self._roster is None or self._dispatcher is None:
            return
        key = RouteKey(self._ctx.database_name, collection)
        self._roster.unwatch(key)
        self._dispatcher.cancel(key)
        self._backoff.pop(key, None)

    async def request_scan(self, umbrella: TaskState) -> None:
        """Enqueue a scan+finalize per active-DB registration; complete *umbrella*.

        Replaces the old ``run_in_threadpool(sync_all)`` path: every collection
        rides the queue, so an explicit ``quarry sync`` serializes behind the
        live watch work instead of racing it.  The umbrella task completes when
        every child job reaches a terminal state (the CLI polls it as before).
        """
        with task_terminal(umbrella):
            children: list[TaskState] = []
            if self._started and self._roster is not None:
                name = self._ctx.database_name
                self._roster.ensure_database(name)
                for collection, root in self._roster.registrations(name):
                    children.extend(self._submit_scan(RouteKey(name, collection), root))
            await self._await_children(children)
            umbrella.status = "completed"
            umbrella.results = {"collections": len(children) // 2}

    # -- internals ----------------------------------------------------------

    def _start_database(self, name: str) -> None:
        """Open *name*'s connection and begin watching each of its registrations."""
        roster = self._roster
        if roster is None:
            return
        try:
            roster.ensure_database(name)
            registrations = roster.registrations(name)
        except (OSError, ValueError) as exc:
            logger.warning("watch: cannot start database %s: %s", name, exc)
            return
        for collection, root in registrations:
            self._begin_collection(name, collection, root)

    def _begin_collection(self, database: str, collection: str, root: Path) -> None:
        """Schedule *collection*'s tree and submit its initial scan + finalize."""
        roster = self._roster
        if roster is None:
            return
        resolved = root.resolve()
        key = RouteKey(database, collection)
        roster.watch(key, resolved, partial(self._on_fs_event, key))
        self._submit_scan(key, resolved)

    def _on_fs_event(self, key: RouteKey, event: FsEvent) -> None:
        """Observer-thread callback: filter, then marshal onto the event loop.

        Never propagates: a raising marshal (loop closed mid-shutdown) is logged,
        so the observer thread survives (bug-class 2).
        """
        if self._loop is None or self._dispatcher is None or self._roster is None:
            return
        root = self._roster.resolved_root(key)
        if root is None or not self._accept(root, event.path):
            return
        try:
            self._loop.call_soon_threadsafe(self._dispatcher.feed, key, event)
        except RuntimeError as exc:
            logger.debug("watch: dropped event after loop close: %s", exc)

    @staticmethod
    def _accept(root: Path, path: Path) -> bool:
        """Return whether *path* is an indexable, non-hidden file within *root*.

        Hidden parts are checked *relative to root* — a watched tree may itself
        live under a dotted directory (``~/.config/...``); only dotted segments
        inside the tree (``.git/``, a dotfile) are skipped, matching the scan.
        """
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return False
        try:
            relative = path.relative_to(root)
        except ValueError:
            return False
        return not any(part.startswith(".") for part in relative.parts)

    def _on_batch(self, batch: FlushBatch) -> None:
        """Dispatcher sink: turn one quiescent batch into queue submissions."""
        roster = self._roster
        if roster is None:
            return
        root = roster.resolved_root(batch.key)
        if root is None:
            return  # deregistered while the batch was pending
        db = roster.database_of(batch.key.database)
        settings = roster.settings_of(batch.key.database)
        if batch.bulk:
            self._submit_scan(batch.key, root)
            return
        failed = self._submit_deltas(batch, db, settings, root)
        if failed:
            self._defer(batch.key, failed)
            return
        self._backoff.pop(batch.key, None)  # batch cleared — reset backoff
        self._submit_finalize(batch.key, db, settings)

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
            delete = DocumentDeleteJob(db, settings, batch.key.collection, name)
            if not self._submit(batch.key, delete, "delete"):
                failed.append(FsEvent(path, deleted=True))
        return failed

    def _submit_scan(self, key: RouteKey, root: Path) -> list[TaskState]:
        """Submit a bulk scan then its coalesced finalize; return their task states."""
        roster = self._roster
        if roster is None:
            return []
        db = roster.database_of(key.database)
        settings = roster.settings_of(key.database)
        states: list[TaskState] = []
        scan = CollectionSyncJob(db, settings, key.collection, root)
        states.append(self._submit_tracked(key, scan, "sync"))
        states.append(self._submit_finalize(key, db, settings))
        return states

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
        if self._loop is None:
            return
        delay = self._backoff.get(key, _BACKOFF_BASE_S)
        self._backoff[key] = min(delay * 2, _BACKOFF_MAX_S)
        self._loop.call_later(delay, self._refeed, key, tuple(events))

    def _refeed(self, key: RouteKey, events: Sequence[FsEvent]) -> None:
        """Feed deferred events back through the debouncer for another attempt."""
        if self._dispatcher is None:
            return
        for event in events:
            self._dispatcher.feed(key, event)

    async def _await_children(self, children: list[TaskState]) -> None:
        """Poll child task states until every one reaches a terminal status."""
        while any(child.status not in _TERMINAL for child in children):
            await asyncio.sleep(0.05)

    @staticmethod
    def _document_name(root: Path, path: Path) -> str | None:
        """Return *path*'s registry document name (relative to *root*), or None."""
        try:
            return str(path.relative_to(root))
        except ValueError:
            return None
