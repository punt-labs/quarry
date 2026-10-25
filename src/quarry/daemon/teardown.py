"""The daemon's shutdown sequence: stop producers, drain the queue, drain the tasks."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.task_drain import TaskDrain
from quarry.shutdown_budget import ShutdownBudget

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext

logger = logging.getLogger(__name__)


@final
class Teardown:
    """Stop the daemon's work in the one order that loses none of it, in one budget.

    The watch loop stops FIRST, so no fresh index job enqueues mid-drain.  The
    queue then closes and drains, so a clean shutdown loses no admitted capture;
    closing it is also what tells a running backfill to stop.  The tasks drain
    last, so a scan thread that was told to stop is waited for rather than left
    writing.  The queue and the tasks share the one
    :class:`~quarry.shutdown_budget.ShutdownBudget`, ``ingest_drain_timeout_s``
    (30 s by default): the service stop timeout is that plus a margin, so the
    manager never kills a teardown that is inside its budget.  A thread that
    still outlives it is named, once, at ERROR.
    """

    __slots__ = ("_ctx",)

    _ctx: DaemonContext

    def __new__(cls, ctx: DaemonContext) -> Self:
        self = super().__new__(cls)
        self._ctx = ctx
        return self

    async def run(self) -> None:
        """Stop the watch loop, drain the queue, then the tasks, within one budget."""
        ctx = self._ctx
        budget = ShutdownBudget(ctx.settings.ingest_drain_timeout_s)
        await ctx.aclose_watch_loop()
        await ctx.ingest_queue.aclose(drain_timeout=budget.remaining())
        # TaskDrain logs its own stragglers; shutdown goes on without them.
        with suppress(TimeoutError):
            await TaskDrain(ctx.tasks.tracked()).run(budget.remaining())
        for state in ctx.tasks.holding_threads():
            logger.error(
                "%s task %s: scan thread still running at shutdown",
                state.kind,
                state.task_id,
            )
