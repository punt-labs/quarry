"""The finalize job the queue runs on a ``(database, collection)`` key."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.settlement import Settlement
from quarry.daemon.store_run import StoreRun
from quarry.daemon.terminal import Terminal
from quarry.sync_finalize import SyncFinalizer

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
    last successful finalize is left alone (``SyncFinalizer``).

    Freshness is therefore DB-granularity, not per-collection: the watch loop
    rate-limits this finalize per database (``FinalizeThrottle``), so a
    just-indexed file's vector channel is searchable immediately while its FTS
    coverage lands with the next finalize that finds the table changed — the
    trailing finalize after churn settles, or, as a backstop, the periodic
    reconcile that re-scans every registered collection.

    ``on_rewrite`` is called on the event loop when this run is going to rewrite
    the table, never when it skips, so a skipped finalize cannot cancel or reset
    a trailing finalize that a write armed.
    """

    database: Database
    settings: Settings
    collection: str
    # None: no throttle needs to hear about this finalize (its own trailing run).
    on_rewrite: Callable[[], None] | None = None

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Rewrite-or-tidy off-thread; announce a rewrite before it starts."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        finalizer = SyncFinalizer(self.database.db, self.settings)
        if self.on_rewrite is not None and finalizer.is_rewrite_due():
            self.on_rewrite()
        with Terminal.for_job(state).recording():
            store = StoreRun.start(
                self, state, self._finalize(finalizer), lambda _: Settlement.done({})
            )
            JobLifecycle(state).settle(await store.wait())

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a finalize is not persisted, so a crash loses it.

        What heals the crash window is not this job: a database no finalize has
        covered in this process is treated as changed, so the first finalize
        after a daemon start rewrites its table (``TableChanges``).
        """
        return None

    @staticmethod
    def _finalize(finalizer: SyncFinalizer) -> Callable[[], None]:
        """Return the store-thread body running *finalizer* (see SAFETY below)."""
        # SAFETY: create_fts_index / optimize touch the shared physical table
        # WITHOUT _write_lock, so per-(database, collection) FIFO ordering is NOT
        # enough — two collections in one database would race their rebuilds. It
        # is safe only because EMBED_CONCURRENCY_CEILING == 1: the global embed
        # gate serializes every job's run() body, so no two finalizes overlap. If
        # that ceiling is ever raised for embed parallelism, finalize must
        # serialize per-DATABASE (per physical table), not per-(database, collection).
        # The changed-since-last-finalize record is itself lock-safe, so a second
        # concurrent finalize would only repeat a rewrite, never lose a write.
        return finalizer.run
