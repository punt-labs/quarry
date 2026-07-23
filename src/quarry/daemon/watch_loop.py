"""The always-on filesystem watch loop: a producer onto the DES-042 queue.

One ``WatchLoop`` per daemon watches every registered directory across every
database in the roster (DES-045).  It owns the *lifecycle* (start/stop, per-tree
scheduling, roster startup) and marshals observer-thread events onto the event
loop; the *producer* half — turning a debounced batch or a scan into IngestUnits
on the per-``(database, collection)`` queue — lives in
:class:`~quarry.daemon.watch_submit.WatchSubmitter`.  It writes no LanceDB table
itself and invents no second queue, inheriting the whole serialization stack
(DES-042 FIFO → DES-034 progressive commit → DES-026 WAL).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.debounce import DebouncedDispatcher
from quarry.daemon.fs_events import NullFsEventSource
from quarry.daemon.fs_watchdog import WatchdogSource
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import task_terminal
from quarry.daemon.watch_roster import WatchRoster
from quarry.daemon.watch_submit import WatchSubmitter

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.fs_events import FsEvent, FsEventSource
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"completed", "failed"})

# request_scan polls its child jobs to completion.  The deadline bounds the poll
# so a wedged threadpool child cannot spin the umbrella (and the CLI's poll)
# forever; an explicit sync exceeding it is pathological, not normal timing.
_SCAN_POLL_S = 0.2
_SCAN_DEADLINE_S = 3600.0


@final
class WatchLoop:
    """Watch every roster database's registered trees and feed the ingest queue."""

    __slots__ = (
        "_ctx",
        "_dispatcher",
        "_loop",
        "_roster",
        "_safety_task",
        "_source",
        "_started",
        "_submitter",
    )

    _ctx: DaemonContext
    _source: FsEventSource | None
    _roster: WatchRoster | None
    _dispatcher: DebouncedDispatcher | None
    _submitter: WatchSubmitter | None
    _loop: asyncio.AbstractEventLoop | None
    _safety_task: asyncio.Task[None] | None
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
        self._submitter = None
        self._loop = None
        self._safety_task = None
        self._started = False
        return self

    async def start(self) -> None:
        """Build the roster (always) and, if enabled, the fs-watch observer.

        The roster + submitter are built unconditionally so an explicit ``quarry
        sync`` enqueues scans regardless of ``watch_enabled`` — the queue is
        always up; only the always-on fs-watch observer is gated (DES-045:
        watch_enabled gates the observer, not on-demand sync).
        """
        settings = self._ctx.settings
        self._loop = asyncio.get_running_loop()
        if self._source is None:
            self._source = (
                WatchdogSource(
                    use_polling=settings.watch_use_polling,
                    poll_interval_s=settings.watch_poll_interval_s,
                )
                if settings.watch_enabled
                else NullFsEventSource()
            )
        self._roster = WatchRoster(
            self._source,
            active_db=self._ctx.database_name,
            active_database=self._ctx.database,
            base_settings=settings,
        )
        self._submitter = WatchSubmitter(self._ctx, self._roster, self._loop)
        if not settings.watch_enabled:
            logger.info("watch: observer disabled; on-demand sync still enqueues")
            return
        # The dispatcher's sink is the submitter; the submitter re-arms shed
        # events through the dispatcher — so create the sink first, then bind.
        self._dispatcher = DebouncedDispatcher(
            self._loop,
            debounce_s=settings.watch_debounce_s,
            max_delay_s=settings.watch_max_delay_s,
            bulk_threshold=settings.watch_bulk_threshold,
            sink=self._submitter.on_batch,
        )
        self._submitter.bind(self._dispatcher)
        self._started = True
        for name in self._roster.roster_names():
            self._start_database(name)
        if settings.watch_safety_scan_s > 0:
            self._safety_task = self._loop.create_task(self._safety_loop())

    async def stop(self) -> None:
        """Tear down the observer + roster before the queue drain (any watch state)."""
        if self._roster is None:  # start() never ran
            return
        self._started = False
        if self._safety_task is not None:
            self._safety_task.cancel()
            # Await so the task is fully retired (a bare cancel() can leave
            # "Task was destroyed but it is pending").
            with contextlib.suppress(asyncio.CancelledError):
                await self._safety_task
        if self._submitter is not None:
            self._submitter.cancel_pending()  # cancel outstanding backoff re-arms
        if self._dispatcher is not None:
            self._dispatcher.cancel_all()
        self._roster.unwatch_all()
        self._roster.close()  # drop sibling conns so a restart can't leak them
        if self._source is not None:
            # stop() joins the observer thread — off the loop so shutdown never blocks.
            await run_in_threadpool(self._source.stop)
        # Drop the built collaborators so a subsequent start() rebuilds cleanly —
        # a joined watchdog observer cannot be restarted (reusing it watches
        # nothing).  A fresh start() reconstructs source + roster.
        self._safety_task = None
        self._source = self._roster = self._dispatcher = self._submitter = None

    def start_watching(self, collection: str, resolved_root: Path) -> None:
        """Begin watching *collection* in the active database + submit its scan."""
        if not self._started:
            return
        self._begin_collection(self._ctx.database_name, collection, resolved_root)

    def stop_watching(self, collection: str) -> None:
        """Stop watching *collection* in the active database (before purge)."""
        if not self._started:
            return
        self._teardown(RouteKey(self._ctx.database_name, collection))

    def _teardown(self, key: RouteKey) -> None:
        """Unwatch *key*'s tree and drop its pending + backoff state."""
        if self._roster is None or self._dispatcher is None:
            return
        self._roster.unwatch(key)
        self._dispatcher.cancel(key)
        if self._submitter is not None:
            self._submitter.forget(key)

    def defer_purge(self, collection: str) -> None:
        """Retry a failed subsume-purge on the next reconcile (durability backstop).

        The register route calls this when a subsume-purge's admission failed; the
        reconcile drains the deferred set, so an orphaned collection is cleaned up
        even though its watch was already torn down.
        """
        if self._submitter is not None:
            self._submitter.defer_purge(RouteKey(self._ctx.database_name, collection))

    async def request_scan(self, umbrella: TaskState) -> None:
        """Enqueue a scan+finalize per active-DB registration; complete *umbrella*.

        Every collection rides the queue rather than a direct threadpool sync,
        so an explicit ``quarry sync`` serializes behind the
        live watch work.  The umbrella reflects the children: it completes only
        when all reach ``completed``, and fails (with a count) if any child was
        shed/failed or the poll deadline hit — no silent success.
        """
        with task_terminal(umbrella):
            children = self._submit_all_scans()
            timed_out = await self._await_children(children)
            WatchSubmitter.summarize_scan(umbrella, children, timed_out=timed_out)

    def _submit_all_scans(self) -> list[TaskState]:
        """Scan+finalize every active-DB registration (runs even if observer off)."""
        roster, submitter = self._roster, self._submitter
        if roster is None or submitter is None:  # start() never ran
            return []
        name = self._ctx.database_name
        roster.ensure_database(name)
        children: list[TaskState] = []
        for collection, root in roster.registrations(name):
            children.extend(submitter.submit_scan(RouteKey(name, collection), root))
        return children

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
        if self._roster is None or self._submitter is None:
            return
        resolved = root.resolve()
        key = RouteKey(database, collection)
        # A re-registration makes the collection live again — cancel any stale
        # orphan-purge immediately so drain never wipes the fresh chunks.
        self._submitter.discard_pending_purge(key)
        self._roster.watch(key, resolved, partial(self._on_fs_event, key))
        self._submitter.submit_scan(key, resolved)

    def _on_fs_event(self, key: RouteKey, event: FsEvent) -> None:
        """Observer-thread callback: filter, then marshal onto the event loop.

        Never propagates: a raising marshal (loop closed mid-shutdown) is logged,
        so the observer thread survives (bug-class 2).
        """
        if self._loop is None or self._dispatcher is None or self._roster is None:
            return
        root = self._roster.resolved_root(key)
        # Observer thread stays cheap: only a lexical dot-segment reject here (no
        # resolve, no .gitignore reads), so a hot writer never backs up the
        # observer queue.  The authoritative filter — suffix (at the source),
        # symlink-escape, and ignore rules — runs post-debounce in the submitter.
        if root is None or self._has_hidden_segment(root, event.path):
            return
        try:
            self._loop.call_soon_threadsafe(self._dispatcher.feed, key, event)
        except RuntimeError as exc:
            logger.debug("watch: dropped event after loop close: %s", exc)

    @staticmethod
    def _has_hidden_segment(root: Path, path: Path) -> bool:
        """Return whether *path* has a dot-segment inside *root* (a cheap reject)."""
        try:
            relative = path.relative_to(root)
        except ValueError:
            return True
        return any(part.startswith(".") for part in relative.parts)

    async def _await_children(self, children: list[TaskState]) -> bool:
        """Poll children to a terminal status; return True if the deadline hit.

        A bounded deadline stops a wedged threadpool child from spinning the
        umbrella task — and the CLI's poll — forever.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SCAN_DEADLINE_S
        while any(child.status not in _TERMINAL for child in children):
            if loop.time() >= deadline:
                return True
            await asyncio.sleep(_SCAN_POLL_S)
        return False

    async def _safety_loop(self) -> None:
        """Periodically reconcile the roster (``watch_safety_scan_s``)."""
        interval = self._ctx.settings.watch_safety_scan_s
        try:
            while self._started:
                await asyncio.sleep(interval)
                if self._started:
                    self._reconcile()
        except asyncio.CancelledError:
            return

    def _reconcile(self) -> None:
        """A full disk-vs-registry pass over EVERY registered collection.

        The backstop that retires quarry-uae: each collection is re-scanned via a
        ``CollectionSyncJob`` (its ``sync_collection`` ingests new/changed and
        deletes gone documents), self-healing a removed directory, a shed FTS
        finalize, and unwatchable (``None``-handle) or new trees — regardless of
        live watch state.  Never propagates (a bad registry read is logged).
        """
        roster, submitter = self._roster, self._submitter
        if roster is None or submitter is None:
            return
        watched, current, complete = self._sync_enumerated(roster, submitter)
        # Removals require a COMPLETE enumeration: a partial `current` (a registry
        # read raised partway) would make a live collection look absent, so
        # tearing down `watched - current` or purging by `current` could destroy a
        # live watch or its chunks.  Skip both this cycle — a stale watch that
        # lingers one cycle self-heals on the next full reconcile; a wiped live
        # collection does not.  The add/rescan already ran for what enumerated.
        if not complete:
            return
        # Tear down watches whose registration disappeared from disk — a
        # removed/renamed directory fires no delete event, so its observer would
        # otherwise persist forever.
        for gone in watched - current:
            self._teardown(gone)
        # Re-attempt any subsume-purge a full queue rejected — the one backstop
        # for orphan chunks a gone collection's teardown never cleans.  A key that
        # is live again (re-registered) is dropped, never purged, so the drain
        # cannot wipe a re-created collection's chunks.
        submitter.drain_pending_purges(live=current)

    def _sync_enumerated(
        self, roster: WatchRoster, submitter: WatchSubmitter
    ) -> tuple[set[RouteKey], set[RouteKey], bool]:
        """Add/rescan every enumerated collection; return (watched, live, complete).

        ``complete`` is False when a registry read raised partway: ``current`` is
        then only a partial view of what is registered, so the caller must skip
        every removal action.  Never propagates — a bad read is logged.
        """
        current: set[RouteKey] = set()
        try:
            watched = set(roster.keys())
            for name in roster.roster_names():
                roster.ensure_database(name)
                for collection, root in roster.registrations(name):
                    key = RouteKey(name, collection)
                    current.add(key)
                    if key not in watched:
                        self._begin_collection(name, collection, root)
                    else:
                        submitter.submit_scan(key, root.resolve())
        except (OSError, ValueError) as exc:
            logger.warning("watch: safety-scan reconcile failed: %s", exc)
            return set(), current, False
        return watched, current, True
