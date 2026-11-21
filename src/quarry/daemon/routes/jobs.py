"""Enqueue route jobs and record the outcomes of background deletes."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from quarry.daemon.route_key import RouteKey
from quarry.daemon.terminal import Terminal

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.task_state import TaskState


@final
class RouteJobs:
    """Coordinate route task responses with daemon queue and task state."""

    @staticmethod
    def submit(ctx: DaemonContext, job: IngestUnit, state: TaskState) -> JSONResponse:
        """Enqueue a job on its collection's FIFO worker, returning 202 or 503.

        The queue serializes ingest per collection and bounds embed concurrency
        (DES-042). A full queue returns a retriable 503 and drops the task record
        so no orphan lingers in ``queued``. The response stays generic because
        ``remember``/``ingest`` have no spooled local artifact to recover.
        """
        key = RouteKey(ctx.database_name, job.collection)
        if not ctx.ingest_queue.try_submit(key, job, state):
            ctx.tasks.drop(state)
            return JSONResponse(
                {
                    "error": "ingest queue full; request not accepted, retry shortly",
                    "status": "rejected",
                },
                status_code=503,
            )
        return JSONResponse(
            {"task_id": state.task_id, "status": "accepted"},
            status_code=202,
        )

    @staticmethod
    async def run_delete(
        state: TaskState,
        delete_call: Callable[[], int],
        name: str,
        kind_label: str,
    ) -> None:
        """Run a document or collection delete off-thread and record its count."""
        with Terminal.for_task(state).recording():
            count = await run_in_threadpool(delete_call)
            state.complete({"deleted": count, "name": name, "type": kind_label})
