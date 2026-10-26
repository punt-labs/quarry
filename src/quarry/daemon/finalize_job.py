"""Collection-table lifecycle jobs the queue runs on a ``(database, collection)`` key.

Both ride the DES-042 per-``(database, collection)`` FIFO worker so they serialize
behind that collection's in-flight index/scan jobs:

- :class:`CollectionFinalizeJob` rebuilds the FTS index once per quiescent batch.
  ``create_fts_index(replace=True)`` leaks a descriptor per generation on the
  daemon's long-lived connection (quarry-0dss), so the watch loop MUST NOT rebuild
  per changed file; coalescing to one post-quiescence pass keeps the fd count flat
  (DES-045 §9, Decision 4) while the FTS lags the vector channel by one batch.
- :class:`CollectionPurgeJob` deletes every chunk of a *deregistered* collection.
  Running FIFO behind any admitted ``FileIndexJob`` for the collection is what
  makes the deregister purge safe: a queued insert re-inserts *before* the purge
  deletes, never after, so no orphan chunk survives with no directory backing it
  (DES-045; the single-writer invariant a direct out-of-queue delete would break).
  It deletes by *collection*, not a document list, so a racing insert of a
  document unknown at deregister time is still purged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.task_terminal import TaskTerminal
from quarry.sync_finalize import SyncFinalizer
from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord
    from quarry.daemon.tasks import TaskState
    from quarry.db import Database


@dataclass(frozen=True, slots=True)
class CollectionFinalizeJob:
    """Rebuild the FTS index and optimize one database's table.

    ``collection`` is only the routing key — the rebuild is table-wide — so the
    job serializes behind that collection's file jobs on the per-``(database,
    collection)`` FIFO worker.  Freshness is therefore DB-granularity, not
    per-collection: the watch loop rate-limits this finalize per database
    (``FinalizeThrottle``), so a just-indexed file's vector channel is searchable
    immediately while its FTS coverage lands with the next finalize for that
    database — the trailing finalize after churn settles, or, as a backstop, the
    periodic reconcile that re-scans every registered collection (unthrottled).
    """

    database: Database
    settings: Settings
    collection: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Run the coalesced FTS rebuild + optimize off-thread (DES-045 §9)."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        with TaskTerminal.recording(state):
            await run_in_threadpool(self._finalize)
            JobLifecycle(state).settle_stored({})

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a finalize is idempotent and reruns on the next batch."""
        return None

    def _finalize(self) -> None:
        """Rebuild the index, optimize, push shadows, then GC (SyncFinalizer)."""
        # SAFETY: create_fts_index / optimize touch the shared physical table
        # WITHOUT _write_lock, so per-(database, collection) FIFO ordering is NOT
        # enough — two collections in one database would race their rebuilds. It
        # is safe only because EMBED_CONCURRENCY_CEILING == 1: the global embed
        # gate serializes every job's run() body, so no two finalizes overlap. If
        # that ceiling is ever raised for embed parallelism, finalize must
        # serialize per-DATABASE (per physical table), not per-(database, collection).
        SyncFinalizer(self.database.db, self.settings).run()


@dataclass(frozen=True, slots=True)
class CollectionPurgeJob:
    """Delete every chunk of a deregistered collection, FIFO behind its inserts.

    The deregister route removes the registry rows synchronously, then submits
    this on the collection's FIFO worker so it runs *after* any in-flight
    ``FileIndexJob`` — the insert lands first, this purge clears it, and no
    orphan chunk survives (DES-045).  Deletion is by collection, so a racing
    insert of a document unknown at deregister time is purged too.
    """

    database: Database
    collection: str
    registry_path: Path
    # Skip the execution-time re-check and delete unconditionally. Set ONLY for
    # the self-subsume clean-slate purge (see _purge), never the sweep/deregister
    # purges, which stay marker-gated.
    force: bool = False

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Delete the collection's chunks off-thread, recording the count."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        with TaskTerminal.recording(state):
            deleted = await run_in_threadpool(self._purge)
            JobLifecycle(state).settle_stored({"deleted": deleted})

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a purge has no content to recover."""
        return None

    def _purge(self) -> int:
        """Re-check the registry under one transaction, then purge if still due.

        A purge submitted from a stale sweep snapshot can be raced by a
        re-enable: X is marked pending and its immediate purge shed, the sweep
        snapshots X as an orphan, X is re-registered (clearing the mark, adding
        the directory row) and re-scanned, THEN this stale purge runs. Deleting
        unconditionally would wipe the re-ingested chunks — and permanently,
        since the re-scan's registry rows survive the chunk delete, so the next
        sync sees them as up to date and never re-ingests.

        So re-check at execution time (the analog of the reconcile's live-key
        skip): inside ONE registry transaction, purge ONLY if the collection is
        still marked and neither registered nor retained. Once the chunks are
        gone the mark is spent, so clearing it keeps the closed set tight and
        stops a future collection re-using the name from being swept on it.

        ``force`` bypasses the still-pending/registered checks (the self-subsume
        clean-slate purge MUST delete the widened parent's OLD narrower-root
        chunks even though it re-registered the same name), but it NEVER bypasses
        the retained guard: retained is an absolute invariant. A concurrent
        keep-data deregister can mark the collection retained (an independent
        commit, no FIFO slot) while a force purge is queued; deleting then would
        wipe the chunks keep-data was asked to preserve. So retained is checked
        standalone and always honored, force or not.
        """
        conn = SyncRegistry(self.registry_path)
        try:
            conn.execute("BEGIN")
            retained = self.collection in conn.markers.list_retained()
            superseded = not self.force and self._superseded_excluding_retained(conn)
            if retained or superseded:
                conn.commit()  # retained / re-registered / cleared → do NOT delete
                return 0
            deleted = self.database.store.delete_collection(self.collection)
            conn.markers.clear_pending(self.collection)
            conn.commit()
        finally:
            conn.close()
        return deleted

    def _superseded_excluding_retained(self, conn: SyncRegistry) -> bool:
        """Return whether the purge is superseded by re-registration or a spent mark.

        Reads under the caller's open transaction (one snapshot).  Retained is the
        caller's standalone absolute guard, so it is deliberately NOT re-queried
        here — one ``list_retained()`` per purge, not two.
        """
        still_pending = self.collection in conn.markers.pending()
        registered = conn.get_registration(self.collection) is not None
        return not still_pending or registered
