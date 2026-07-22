"""The watch loop's post-quiescence finalize job: one coalesced FTS rebuild.

``create_fts_index(replace=True)`` leaks a file descriptor per generation on the
daemon's long-lived connection (quarry-0dss), so the watch loop MUST NOT rebuild
the FTS index per changed file.  Instead, once a ``(database, collection)`` goes
quiescent (its debounce drained with no pending paths), the loop submits ONE
:class:`CollectionFinalizeJob` behind that collection's file jobs on the same
FIFO worker.  It runs the existing post-sync :class:`SyncFinalizer` (index
rebuild + optimize + shadow push + GC, DES-023), so the FTS lags the vector
channel by one quiescent batch — never one rebuild per file — and the daemon's
open-fd count plateaus (DES-045 §9, Decision 4; proven by the resource-invariant
test).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

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
        await run_in_threadpool(self._finalize)
        state.status = "completed"

    def spool_record(self) -> SpoolRecord | None:
        """Return ``None``: a finalize is idempotent and reruns on the next batch."""
        return None

    def _finalize(self) -> None:
        """Rebuild the index, optimize, push shadows, then GC (SyncFinalizer)."""
        SyncFinalizer(self.database.db, self.settings).run()
