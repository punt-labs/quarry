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

from quarry.daemon.tasks import task_terminal
from quarry.sync_finalize import SyncFinalizer

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord
    from quarry.daemon.tasks import TaskState
    from quarry.db import Database


@dataclass(frozen=True, slots=True)
class CollectionFinalizeJob:
    """Rebuild the FTS index and optimize one database's table, once per batch.

    ``collection`` is only the routing key — the rebuild is table-wide — so the
    job serializes behind that collection's file jobs on the per-``(database,
    collection)`` FIFO worker, guaranteeing the FTS reflects every just-indexed
    file.
    """

    database: Database
    settings: Settings
    collection: str

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Run the coalesced FTS rebuild + optimize off-thread (DES-045 §9)."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        with task_terminal(state):
            await run_in_threadpool(self._finalize)
            state.status = "completed"

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

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Delete the collection's chunks off-thread, recording the count."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        with task_terminal(state):
            deleted = await run_in_threadpool(
                self.database.store.delete_collection, self.collection
            )
            state.status = "completed"
            state.results = {"deleted": deleted}

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a purge has no content to recover."""
        return None
