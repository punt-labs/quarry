"""The backfill background task: scan on a worker thread, write through the FIFO."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry import backfill
from quarry.daemon.bounded_scan import BoundedScan
from quarry.daemon.queued_ingest import DEFAULT_QUEUE_POLICY
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter
from quarry.daemon.run_report import RunReport
from quarry.daemon.thread_call import ThreadCall
from quarry.stop_signal import StopSignal

if TYPE_CHECKING:
    from quarry.daemon.backfill_args import BackfillArgs
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.tasks import TaskState


@final
class BackfillTask:
    """Run one transcript backfill over the daemon's database and collection FIFOs.

    The scan reads transcripts and the store on a worker thread, as it always
    did; every write goes through :class:`QueuedTranscriptWriter`, the single
    writer each collection has, so backfill holds no connection of its own.  The
    run's stop signal is set (with its reason) when the queue closes and when
    this task is cancelled (:class:`BoundedScan`).  The task ends ``failed``
    when it stored nothing and left something undone, else ``completed``.
    """

    # Seconds to wait for the scan thread after a stop: it can still be busy
    # only in one step it cannot interrupt (a read, a scrub, a capture write),
    # seconds even on a slow disk, so half a minute fires only on a hang.
    STOP_BOUND_S: ClassVar[float] = 30.0

    __slots__ = ("_bound_s", "_ctx")

    _bound_s: float
    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext, bound_s: float = STOP_BOUND_S) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        self._bound_s = bound_s
        return self

    async def run(self, state: TaskState, args: BackfillArgs) -> None:
        """Backfill as *args* says, recording the stats and the outcome on *state*."""
        config = backfill.BackfillConfig(
            dry_run=args.dry_run,
            collection_override=args.collection,
            project_filter=args.project,
            limit=args.limit,
        )
        stop = StopSignal()
        call = ThreadCall.here(stop, DEFAULT_QUEUE_POLICY.total_s)
        report = RunReport(state, stop, dry_run=args.dry_run)
        runtime = backfill.BackfillRuntime(
            self._ctx.database,
            QueuedTranscriptWriter(self._ctx, call),
            self._ctx.settings.registry_path,
            stop,
            report.progress,
        )
        with self._ctx.ingest_queue.gate.stopping(stop):
            await BoundedScan(stop, self._bound_s).run(
                partial(backfill.Backfill.sessions, config, runtime),
                state,
                report,
            )
