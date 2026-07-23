"""Collection-wide chunk purge routed through the per-collection FIFO worker."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.finalize_job import CollectionPurgeJob
from quarry.daemon.route_key import RouteKey

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)

# A purge polls its queued delete job to completion.  A full queue is transient
# (workers drain), and the purge MUST run — the registry rows are already gone —
# so submission retries within a bounded window before failing.
_PURGE_TERMINAL = frozenset({"completed", "failed"})
_PURGE_POLL_S = 0.05
_PURGE_SUBMIT_DEADLINE_S = 30.0


@final
class CollectionPurger:
    """Delete a collection's chunks through its FIFO worker, behind queued inserts.

    Routing the purge onto the per-``(database, collection)`` FIFO makes it run
    behind any already-admitted ``FileIndexJob`` for the same collection, so a
    queued insert can never resurrect chunks *after* the purge — the single-writer
    invariant a direct out-of-queue ``delete_document`` would violate.
    """

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    async def purge(self, collection: str, label: str) -> TaskState:
        """Purge *collection*'s chunks through its FIFO worker; poll to completion.

        Return the queued purge task so the caller can read ``deleted`` or detect
        a failed admission — the outcome an observable cleanup depends on.
        """
        purge = self._ctx.tasks.begin(label)
        job = CollectionPurgeJob(self._ctx.database, collection)
        key = RouteKey(self._ctx.database_name, collection)
        await self._admit(key, job, purge)
        while purge.status not in _PURGE_TERMINAL:
            await asyncio.sleep(_PURGE_POLL_S)
        return purge

    async def _admit(
        self, key: RouteKey, job: CollectionPurgeJob, purge: TaskState
    ) -> None:
        """Admit the purge, retrying a transiently-full queue within the deadline.

        The registry rows are already gone, so the purge MUST run or the
        collection's chunks orphan.  A full queue drains as workers finish, so
        submission retries until the deadline before marking the purge failed.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PURGE_SUBMIT_DEADLINE_S
        while not self._ctx.ingest_queue.try_submit(key, job, purge):
            if loop.time() >= deadline:
                self._ctx.tasks.drop(purge)
                purge.status = "failed"
                purge.error = "ingest queue full; purge not admitted"
                return
            await asyncio.sleep(_PURGE_POLL_S)
