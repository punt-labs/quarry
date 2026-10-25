"""The backfill background task: scan on a worker thread, write through the FIFO."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Self, final

from quarry import backfill
from quarry.daemon.guarded_scan import GuardedScan
from quarry.daemon.queued_ingest import DEFAULT_QUEUE_POLICY
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.daemon.thread_call import ThreadCall
from quarry.stop_signal import StopSignal

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


@final
class BackfillTask:
    """Run one transcript backfill over the daemon's database and collection FIFOs.

    The scan reads transcripts and the store on a worker thread, as it always
    did; every write goes through :class:`QueuedTranscriptWriter`, the single
    writer each collection has, so backfill holds no connection of its own.  The
    run's stop signal is set (with its reason) when the queue closes and when
    this task is cancelled (:class:`GuardedScan`).  The task ends ``failed``
    when it stored nothing and left something undone, else ``completed``.
    """

    __slots__ = ("_ctx",)

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    async def run(
        self,
        state: TaskState,
        *,
        dry_run: bool,
        collection: str,
        project: str,
        limit: int,
    ) -> None:
        """Backfill, recording the stats and the outcome on *state*."""
        config = backfill.BackfillConfig(
            dry_run=dry_run,
            collection_override=collection,
            project_filter=project,
            limit=limit,
        )
        stop = StopSignal()
        call = ThreadCall.here(stop, DEFAULT_QUEUE_POLICY.total_s)
        writer = QueuedTranscriptWriter(self._ctx, call)
        runtime = backfill.BackfillRuntime(
            self._ctx.database, writer, self._ctx.settings.registry_path, stop
        )
        with self._ctx.ingest_queue.gate.stopping(stop):
            stats = await GuardedScan(stop).run(
                partial(backfill.Backfill.sessions, config, runtime)
            )
        state.results = stats.as_results(dry_run=dry_run)
        if stats.stored_nothing:
            state.status = "failed"
            state.error = f"backfill stored nothing: {stats.errors[0]}"
        else:
            state.status = "completed"
