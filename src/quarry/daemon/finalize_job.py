"""The collection purge job the queue runs on a ``(database, collection)`` key.

:class:`CollectionPurgeJob` deletes every chunk of a *deregistered* collection.
It rides the per-``(database, collection)`` FIFO worker so it serializes behind
that collection's in-flight index/scan jobs.  Running FIFO behind any admitted
``FileIndexJob`` for the collection is what makes the deregister purge safe: a
queued insert re-inserts *before* the purge deletes, never after, so no orphan
chunk survives with no directory backing it (the single-writer invariant a
direct out-of-queue delete would break).  It deletes by *collection*, not a
document list, so a racing insert of a document unknown at deregister time is
still purged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.purge_recheck import PurgeRecheck
from quarry.daemon.settlement import Settlement
from quarry.daemon.store_run import StoreRun
from quarry.daemon.terminal import Terminal

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord
    from quarry.daemon.task_state import TaskState
    from quarry.db import Database


@dataclass(frozen=True, slots=True)
class CollectionPurgeJob:
    """Delete every chunk of a deregistered collection, FIFO behind its inserts.

    The deregister route removes the registry rows synchronously, then submits
    this on the collection's FIFO worker so it runs *after* any in-flight
    ``FileIndexJob`` — the insert lands first, this purge clears it, and no
    orphan chunk survives.  Deletion is by collection, so a racing
    insert of a document unknown at deregister time is purged too.
    """

    database: Database
    collection: str
    registry_path: Path
    # Skip the execution-time re-check and delete unconditionally. Set ONLY for
    # the self-subsume clean-slate purge (see PurgeRecheck), never the sweep/deregister
    # purges, which stay marker-gated.
    force: bool = False

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Delete the collection's chunks off-thread, recording the count."""
        del ctx  # this job carries its own database; the queue's ctx is unused
        with Terminal.for_job(state).recording():
            store = StoreRun.start(
                self,
                state,
                self._purge,
                lambda deleted: Settlement.done({"deleted": deleted}),
            )
            JobLifecycle(state).settle(await store.wait())

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a purge has no content to recover."""
        return None

    def _purge(self) -> int:
        """Re-check the registry under one transaction, then purge if still due."""
        return PurgeRecheck(
            self.registry_path, self.collection, force=self.force
        ).delete_if_due(self.database.store)
