"""The periodic disk-vs-registry reconcile and the durable orphan-purge backstop.

Extracted from :class:`~quarry.daemon.watch_loop.WatchLoop`: the loop owns
lifecycle, watch scheduling, and observer-thread marshaling, while this owns the
safety-scan reconcile (re-scan every registered collection; tear down a watch
whose registration vanished) and the purge backstop — deferred subsume/deregister
purges retried until the queue admits them, plus (once wired) a disk-derived
sweep that deletes chunks of any collection that is neither registered nor
retained.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.finalize_job import CollectionPurgeJob
from quarry.daemon.route_key import RouteKey
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcilerDeps:
    """The live collaborators a reconcile pass needs, bound once WatchLoop starts.

    ``begin`` and ``teardown`` are WatchLoop's own watch-management methods — the
    reconciler drives them but does not own watch scheduling.
    """

    ctx: DaemonContext
    roster: WatchRoster
    submitter: WatchSubmitter
    begin: Callable[[str, str, Path], None]
    teardown: Callable[[RouteKey], None]


@final
class WatchReconciler:
    """Reconcile the roster on a timer and back-stop orphaned collection chunks."""

    __slots__ = ("_deps", "_pending_purges")

    _deps: ReconcilerDeps
    _pending_purges: set[RouteKey]

    def __new__(cls, deps: ReconcilerDeps) -> Self:
        self = super().__new__(cls)
        self._deps = deps
        self._pending_purges = set()
        return self

    def defer_purge(self, key: RouteKey) -> None:
        """Queue a failed purge for reconcile-driven re-admission.

        A subsume/deregister purge the saturated queue rejected leaves orphan
        chunks with no other backstop, so the collection is retried until the
        queue admits the delete.
        """
        self._pending_purges.add(key)

    def discard_pending_purge(self, key: RouteKey) -> None:
        """Cancel a deferred purge because *key* is registered (and live) again.

        A re-registration makes a collection live at a new root before the next
        reconcile; its earlier orphans are moot and purging would wipe the live
        collection's fresh chunks — so a re-watch supersedes the stale purge.
        """
        self._pending_purges.discard(key)

    async def run_safety_loop(self) -> None:
        """Reconcile every ``watch_safety_scan_s`` until cancelled."""
        interval = self._deps.ctx.settings.watch_safety_scan_s
        try:
            while True:
                await asyncio.sleep(interval)
                await self.run_once()
        except asyncio.CancelledError:
            return

    async def run_once(self) -> None:
        """A full disk-vs-registry pass: rescan, tear down removed, purge orphans.

        Removals require a COMPLETE enumeration: a partial ``current`` (a registry
        read raised partway) would make a live collection look absent, so tearing
        down ``watched - current`` or purging by ``current`` could destroy a live
        watch or its chunks.  A partial cycle skips every removal; the next full
        reconcile self-heals.
        """
        watched, current, complete = self._sync_enumerated()
        if not complete:
            return
        for gone in watched - current:
            self._deps.teardown(gone)
        self._drain_pending(live=current)
        await self._sweep_orphans(current)

    async def _sweep_orphans(self, live: set[RouteKey]) -> None:
        """Purge chunks of any collection neither registered nor retained.

        The durable backstop: orphans are derived from actual DB + registry state
        every reconcile, so a shed/failed purge (deregister OR subsume) is cleaned
        up even across a restart, without relying on the in-process pending set.

        Data-safety invariant (I6): the swept set is a subset of
        chunks minus (registered union retained).  ``registered`` comes from the
        live roster snapshot and ``retained`` from the durable keep-data marker,
        so a registered or operator-kept collection is NEVER swept.  The blocking
        chunk scan + registry read run off the loop.
        """
        ctx = self._deps.ctx
        active = ctx.database_name
        registered = {key.collection for key in live if key.database == active}
        orphans = await run_in_threadpool(self._read_orphans, registered)
        for collection in orphans:
            task = ctx.tasks.begin("orphan-sweep-purge")
            job = CollectionPurgeJob(ctx.database, collection)
            key = RouteKey(active, collection)
            if not ctx.ingest_queue.try_submit(key, job, task):
                ctx.tasks.drop(task)  # full queue → the next reconcile re-sweeps

    def _read_orphans(self, registered: set[str]) -> set[str]:
        """Off-thread: collections with chunks but neither registered nor retained.

        Pure reads through ``ctx.database`` (cross-thread-safe, as the queue
        workers already share it) and a fresh registry connection — NO roster
        access, so nothing races the loop thread's watch scheduling.
        """
        ctx = self._deps.ctx
        chunk_cols = {c["collection"] for c in ctx.database.catalog.list_collections()}
        conn = SyncRegistry(ctx.settings.registry_path)
        try:
            retained = set(conn.list_retained())
        finally:
            conn.close()
        return chunk_cols - registered - retained

    def _sync_enumerated(self) -> tuple[set[RouteKey], set[RouteKey], bool]:
        """Add/rescan every enumerated collection; return (watched, live, complete).

        ``complete`` is False when a registry read raised partway — ``current`` is
        then only a partial view, so the caller must skip every removal action.
        Never propagates: a bad read is logged.
        """
        roster, submitter = self._deps.roster, self._deps.submitter
        current: set[RouteKey] = set()
        try:
            watched = set(roster.keys())
            for name in roster.roster_names():
                roster.ensure_database(name)
                for collection, root in roster.registrations(name):
                    key = RouteKey(name, collection)
                    current.add(key)
                    if key not in watched:
                        self._deps.begin(name, collection, root)
                    else:
                        submitter.submit_scan(key, root.resolve())
        except (OSError, ValueError) as exc:
            logger.warning("watch: safety-scan reconcile failed: %s", exc)
            return set(), current, False
        return watched, current, True

    def _drain_pending(self, live: set[RouteKey]) -> None:
        """Re-submit each deferred purge whose collection is no longer registered.

        A key that is *live* (in the roster) was re-registered after its purge was
        deferred; purging it would destroy the live collection's chunks, so it is
        dropped WITHOUT submitting.  For a still-absent key, admission of the
        ``CollectionPurgeJob`` is the retry's success condition — a still-full
        queue keeps it for the next reconcile.
        """
        if not self._pending_purges:
            return
        ctx = self._deps.ctx
        still: set[RouteKey] = set()
        for key in self._pending_purges:
            if key in live:
                continue
            task = ctx.tasks.begin("subsume-purge-retry")
            job = CollectionPurgeJob(ctx.database, key.collection)
            if not ctx.ingest_queue.try_submit(key, job, task):
                ctx.tasks.drop(task)
                still.add(key)
        self._pending_purges = still
