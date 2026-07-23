"""The contract a job must satisfy to be serialized by the ingest queue.

A unit names the LanceDB collection it writes (so the queue can route it to that
collection's single FIFO worker) and owns its own background execution against a
:class:`~quarry.daemon.tasks.TaskState`.  Every daemon job satisfies it
structurally, so the queue never imports a concrete job type: the content jobs
(inline scrub, URL ingest, web-fetch capture) and the watch-loop jobs
(``FileIndexJob``, ``DocumentDeleteJob``, ``CollectionSyncJob``,
``CollectionFinalizeJob``, ``CollectionPurgeJob`` — DES-045).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord
    from quarry.daemon.tasks import TaskState


class IngestUnit(Protocol):
    """A self-executing ingest job keyed by its target collection.

    ``collection`` is the routing key: two units naming the same collection run
    on one worker in submission order, restoring the single-writer-per-table
    precondition ``progressive_insert`` assumes (DES-034).  ``run`` records its
    own terminal state via ``task_terminal`` — the queue never inspects it.
    """

    @property
    def collection(self) -> str:
        """Return the LanceDB collection this unit writes (the routing key)."""
        ...

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Execute the ingest, recording completion/failure on *state*."""
        ...

    def spool_record(self) -> SpoolRecord | None:
        """Return a recoverable snapshot if this job has no durable client copy.

        ``None`` means a durable client-side artifact already outlives an abort
        (a capture's transcript ``.md``), so the shutdown drain need not spool
        it.  A ``remember`` or plain ``ingest`` has no such artifact and returns
        a scrubbed record so an abort never silently drops admitted knowledge.
        """
        ...
