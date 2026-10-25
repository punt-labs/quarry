"""The backfill background task: scan on a worker thread, write through the FIFO."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry import backfill
from quarry.daemon.queued_transcript_writer import QueuedTranscriptWriter

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext


@final
class BackfillTask:
    """Run one transcript backfill over the daemon's database and collection FIFOs.

    The scan reads transcripts and the store on a worker thread, as it always
    did; every write goes through :class:`QueuedTranscriptWriter`, the single
    writer each collection has, so backfill holds no connection of its own.
    """

    __slots__ = ("_ctx",)

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    async def run(
        self, *, dry_run: bool, collection: str, project: str, limit: int
    ) -> dict[str, object]:
        """Backfill and return the stats as a task's results."""
        config = backfill.BackfillConfig(
            dry_run=dry_run,
            collection_override=collection,
            project_filter=project,
            limit=limit,
        )
        writer = QueuedTranscriptWriter(self._ctx, asyncio.get_running_loop())
        runtime = backfill.BackfillRuntime(
            self._ctx.database, writer, self._ctx.settings.registry_path
        )
        stats = await run_in_threadpool(backfill.backfill_sessions, config, runtime)
        return stats.as_results(dry_run=dry_run)
