"""The contract a job must satisfy to be serialized by the ingest queue.

A unit names the LanceDB collection it writes (so the queue can route it to that
collection's single FIFO worker) and owns its own background execution against a
:class:`~quarry.daemon.tasks.TaskState`.  The three daemon jobs — inline scrub,
URL ingest, and web-fetch capture — all satisfy it structurally, so the queue
never imports a concrete job type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


@runtime_checkable
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
