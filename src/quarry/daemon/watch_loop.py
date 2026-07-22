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
import logging
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.debounce import DebouncedDispatcher
from quarry.daemon.fs_watchdog import WatchdogSource
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import task_terminal
from quarry.daemon.watch_roster import WatchRoster
from quarry.daemon.watch_submit import WatchSubmitter
from quarry.ingestion.pipeline import SUPPORTED_EXTENSIONS

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
        self._submitter = WatchSubmitter(self._ctx, self._roster, self._loop)
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
        if self._submitter is not None:
            self._submitter.forget(key)

    async def request_scan(self, umbrella: TaskState) -> None:
        """Enqueue a scan+finalize per active-DB registration; complete *umbrella*.

        Replaces the old ``run_in_threadpool(sync_all)`` path: every collection
        rides the queue, so an explicit ``quarry sync`` serializes behind the
        live watch work.  The umbrella reflects the children: it completes only
        when all reach ``completed``, and fails (with a count) if any child was
        shed/failed or the poll deadline hit — no silent success.
        """
        with task_terminal(umbrella):
            children: list[TaskState] = []
            roster, submitter = self._roster, self._submitter
            if self._started and roster is not None and submitter is not None:
                name = self._ctx.database_name
                roster.ensure_database(name)
                for collection, root in roster.registrations(name):
                    children.extend(
                        submitter.submit_scan(RouteKey(name, collection), root)
                    )
            timed_out = await self._await_children(children)
            failed = sum(1 for child in children if child.status == "failed")
            umbrella.results = {"collections": len(children) // 2, "failed": failed}
            if timed_out:
                umbrella.status = "failed"
                umbrella.error = "scan timed out before all jobs completed"
            elif failed:
                umbrella.status = "failed"
                umbrella.error = f"{failed} scan job(s) failed"
            else:
                umbrella.status = "completed"

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
