"""The periodic disk-vs-registry reconcile and the durable orphan-purge backstop.

Extracted from :class:`~quarry.daemon.watch_loop.WatchLoop`: the loop owns
lifecycle, watch scheduling, and observer marshaling; this owns the safety-scan
reconcile (re-scan registered collections; tear down a vanished watch) and the
CLOSED-SET purge backstop -- it purges only collections the registry marked in
``pending_purge_collections`` (non-keep-data deregister or subsume eviction) that
still hold chunks, so captures/memories/remember targets are never swept.  Reads
run under one BEGIN snapshot; each queued purge re-checks (``_superseded``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.daemon.finalize_job import CollectionPurgeJob
from quarry.daemon.route_key import RouteKey
from quarry.scratch_paths import ScratchGuard
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

    __slots__ = ("_deps", "_guard", "_pending_purges")

    _deps: ReconcilerDeps
    _pending_purges: set[RouteKey]
    _guard: ScratchGuard

    def __new__(cls, deps: ReconcilerDeps) -> Self:
        self = super().__new__(cls)
        self._deps = deps
        self._pending_purges = set()
        self._guard = ScratchGuard()
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
        await self._sweep_orphans()

    async def _sweep_orphans(self) -> None:
        """Purge chunks of any collection neither registered nor retained.

        The durable backstop: orphans are derived from actual DB + registry state
        every reconcile, so a shed/failed purge (deregister OR subsume) is cleaned
        up even across a restart, without relying on the in-process pending set.

        Data-safety invariant (I6): the swept set is a subset of chunks minus
        (registered union retained).  ``registered`` and ``retained`` are read
        inside ONE registry transaction in :meth:`_read_orphans`, so a concurrent
        register/keep-data deregister is invisible to both reads and no live or
        kept collection is ever misclassified as an orphan (see there).

        This backstop must never die: it is the only cleanup for a shed/failed
        purge, so a read failure of ANY kind fail-closes — log and skip this
        cycle (self-heal next reconcile), never let it escape and kill the loop.
        Skipping errs toward not-deleting, the safe direction.
        """
        ctx = self._deps.ctx
        try:
            orphans = await run_in_threadpool(self._read_orphans)
        except Exception as exc:
            # any type (incl. LanceDB/pyarrow errors outside the stdlib hierarchy)
            # must skip the cycle, never kill the safety loop; fails toward safety.
            logger.warning(
                "watch: orphan sweep read failed, skipping cycle: %s",
                exc,
                exc_info=True,  # traceback: diagnose a production read failure
            )
            return
        active = ctx.database_name
        for collection in orphans:
            task = ctx.tasks.begin("orphan-sweep-purge")
            job = CollectionPurgeJob(
                ctx.database, collection, ctx.settings.registry_path
            )
            key = RouteKey(active, collection)
            if not ctx.ingest_queue.try_submit(key, job, task):
                ctx.tasks.drop(task)  # full queue → the next reconcile re-sweeps

    def _read_orphans(self) -> set[str]:
        """Off-thread: purge-marked collections that still have chunks.

        CLOSED SET, not open world: the swept set is
        ``(pending & chunk_cols) - registered - retained`` -- only collections the
        registry EXPLICITLY marked for purge (a non-keep deregister or a subsume
        eviction) and that still have chunks.  Captures, agent memories, and
        remember targets are never marked, so they are structurally unreachable --
        the open-world ``chunk_cols - registered - retained`` wiped them.  The
        ``registered``/``retained`` subtraction stays as belt-and-suspenders: a
        collection re-registered or kept after being marked is spared even if its
        stale mark lingers.

        The three registry reads (pending, registered, retained) run inside ONE
        explicit transaction so they see a single consistent snapshot.  This is
        load-bearing: Python's sqlite3 opens an implicit transaction only before
        DML, never before a SELECT, so without the ``BEGIN`` a
        register/keep-data-deregister committing between reads could fall through
        the sets.  ``chunk_cols`` is read first from the vector store's catalog (a
        separate database, so not part of the registry transaction); a collection
        that gains chunks between that read and the snapshot is simply picked up
        next reconcile.  Pure reads through ``ctx.database`` and the registry — NO
        roster access, so nothing races the loop thread.
        """
        ctx = self._deps.ctx
        chunk_cols = {c["collection"] for c in ctx.database.catalog.list_collections()}
        conn = SyncRegistry(ctx.settings.registry_path)
        try:
            conn.execute("BEGIN")  # one read snapshot spans every SELECT
            pending = conn.markers.pending()
            registered = {reg.collection for reg in conn.list_registrations()}
            retained = set(conn.markers.list_retained())
            conn.commit()
        finally:
            conn.close()
        return (pending & chunk_cols) - registered - retained

    def _sync_enumerated(self) -> tuple[set[RouteKey], set[RouteKey], bool]:
        """Add/rescan every enumerated collection; return (watched, live, complete).

        ``complete`` is False when enumeration raised partway — ``current`` is
        then only a partial view, so the caller must skip every removal action.
        Never propagates: enumeration touches the roster, the registry, and the
        filesystem (``ensure_database``, ``registrations``, ``resolve``), which can
        raise error types outside the stdlib hierarchy (LanceDB/pyarrow), so it
        catches broadly and fails closed — an escaped error would kill the safety
        loop, stranding every reconcile.  The next full reconcile self-heals.
        """
        roster, submitter = self._deps.roster, self._deps.submitter
        current: set[RouteKey] = set()
        # One sweep for the whole cycle: every collection is rescanned, but the
        # table-wide finalize runs once per database instead of once per
        # collection (which repeated identical optimize+FTS work N times).
        sweep = submitter.sweep()
        try:
            watched = set(roster.keys())
            for name in roster.roster_names():
                roster.ensure_database(name)
                for collection, root in roster.registrations(name):
                    # A temp/scratch root already in the registry (e.g. a stale
                    # /private/tmp entry) is refused here too, so a restart never
                    # enumerates or re-scans it and a vanishing temp tree cannot
                    # thrash the cycle.  Same predicate the watch gate uses.
                    # Resolve once, per-root fail-closed: an unresolvable root
                    # (ELOOP) skips this one registration, never the whole cycle.
                    try:
                        resolved = root.resolve()
                    except (OSError, RuntimeError, ValueError) as exc:
                        logger.warning(
                            "watch: reconcile skipping unresolvable root %s: %s",
                            root,
                            exc,
                        )
                        continue
                    if self._guard.refuses_root(resolved):
                        continue
                    key = RouteKey(name, collection)
                    current.add(key)
                    if key not in watched:
                        self._deps.begin(name, collection, root)
                    else:
                        sweep.add(key, resolved)
        except Exception as exc:
            # error may escape and kill the safety loop; fail closed and self-heal.
            # exc_info: a production enumeration failure needs the traceback.
            logger.warning(
                "watch: safety-scan reconcile failed: %s", exc, exc_info=True
            )
            return set(), current, False
        finally:
            # Finalize whatever WAS scanned, including on the failure path: those
            # collections were rescanned and their FTS would otherwise lag until
            # the next cycle, and a partial cycle is when the self-heal matters
            # most.
            sweep.finish()
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
            job = CollectionPurgeJob(
                ctx.database, key.collection, ctx.settings.registry_path
            )
            if not ctx.ingest_queue.try_submit(key, job, task):
                ctx.tasks.drop(task)
                still.add(key)
        self._pending_purges = still
