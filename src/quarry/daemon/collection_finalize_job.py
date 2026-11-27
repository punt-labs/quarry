"""The finalize job the queue runs on a ``(database, collection)`` key."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.settlement import Settlement
from quarry.daemon.store_run import StoreRun
from quarry.daemon.terminal import Terminal
from quarry.db.lance_connection import LanceConnection
from quarry.table_finalizer import TableFinalizer

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord
    from quarry.daemon.task_state import TaskState
    from quarry.db import Database


@dataclass(frozen=True, slots=True)
class CollectionFinalizeJob:
    """Rewrite one database's table if it changed; otherwise keep it tidy.

    ``collection`` is only the routing key: the finalize is table-wide, so the
    job serializes behind that collection's file jobs on the per-``(database,
    collection)`` FIFO worker.  Whether to rewrite is decided when the job RUNS,
    not when it is submitted: a scan's outcome is unknown at submission, and by
    the time this job reaches the head of the queue every job ahead of it has
    written whatever it was going to write.  A table nobody wrote to since its
    last successful finalize is left alone (``TableFinalizer``).

    Freshness is therefore DB-granularity, not per-collection: the watch loop
    rate-limits this finalize per database (``FinalizeThrottle``), so a
    just-indexed file's vector channel is searchable immediately while its FTS
    coverage lands with the next finalize that finds the table changed — the
    trailing finalize after churn settles, or, as a backstop, the periodic
    reconcile that re-scans every registered collection.

    ``on_rewrite`` is called on the event loop when this run is going to rewrite
    the table, never when it skips, so a skipped sweep finalize cannot cancel or
    reset a trailing finalize that a write armed.  ``on_unsettled`` is called on
    the loop, with the seconds until a retry is allowed, when the table is still
    dirty afterwards (a failure, a back-off, or a write during the run), so a
    retry is scheduled even with the periodic safety scan disabled.
    """

    database: Database
    settings: Settings
    collection: str
    # None: no throttle needs to hear about this finalize (its own trailing run).
    on_rewrite: Callable[[], None] | None = None
    # None: nothing retries this finalize if it leaves the table dirty.
    on_unsettled: Callable[[float], None] | None = None

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Rewrite-or-tidy off-thread; the store thread decides, then reports back."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        loop = asyncio.get_running_loop()
        with Terminal.for_job(state).recording():
            store = StoreRun.start(
                self,
                state,
                lambda: self._finalize(loop),
                lambda _: Settlement.done({}),
            )
            JobLifecycle(state).settle(await store.wait())

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a finalize is not persisted, so a crash loses it.

        What heals the crash window is not this job: a database no finalize has
        covered in this process is treated as changed, so the first finalize
        after a daemon start rewrites its table (``TableChanges``).
        """
        return None

    def _finalize(self, loop: asyncio.AbstractEventLoop) -> None:
        """Run the finalizer on the store thread, reporting to the loop (see SAFETY)."""
        # SAFETY: create_fts_index / optimize touch the shared physical table
        # WITHOUT _write_lock, so per-(database, collection) FIFO ordering is NOT
        # enough — two collections in one database would race their rebuilds. Two
        # things serialize them.  EMBED_CONCURRENCY_CEILING == 1: the global embed
        # gate serializes every job's run() body, so no two finalizes overlap; if
        # that ceiling is ever raised, finalize must serialize per-DATABASE.  And
        # the per-database optimize lock (FinalizeLedger.exclusive), which the
        # finalize rewrite and the operator's manual optimize route
        # (MaintenanceRoutes, which runs outside the embed gate) both take.
        # The decision is made here, on the store thread, once: the record is
        # lock-safe, so a second concurrent finalize would only repeat a rewrite.
        changes = LanceConnection.changes_of(self.database.db)
        finalizer = TableFinalizer(self.database.db, self.settings, changes)
        announce = self._announcer(loop)
        retry_in = finalizer.run(announce)
        if retry_in is not None and self.on_unsettled is not None:
            loop.call_soon_threadsafe(self.on_unsettled, retry_in)

    def _announcer(self, loop: asyncio.AbstractEventLoop) -> Callable[[], None]:
        """Return the call that tells the loop a rewrite is starting."""
        hook = self.on_rewrite

        def announce() -> None:
            if hook is not None:
                loop.call_soon_threadsafe(hook)

        return announce
