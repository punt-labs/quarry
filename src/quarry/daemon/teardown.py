"""The daemon's shutdown sequence: stop producers, drain the queue, drain the tasks."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.task_drain import TaskDrain
from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_budget import ShutdownBudget
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from quarry.daemon.context import DaemonContext

logger = logging.getLogger(__name__)


@final
class Teardown:
    """Stop the daemon's work in the one order that loses none of it, in one budget.

    The watch loop stops FIRST, so no fresh index job enqueues mid-drain.  The
    queue then closes and drains, so a clean shutdown loses no admitted capture;
    closing it is also what tells a running backfill to stop.  The tasks drain
    last, so a scan thread that was told to stop is waited for rather than left
    writing.

    The budget is ``ingest_drain_timeout_s`` (:class:`ShutdownPolicy` splits it).
    With the default 30 s, as an illustration only: the watch-loop stop gets at
    most a fifth of it, the queue drain is guaranteed at least half, a scan thread
    that will not stop is given up on two fifths of it after the stop (so a stuck
    scan normally ends inside the task drain), and the task drain gets the rest,
    its cancel wait a quarter of that.  The service stop timeout is the budget
    plus a margin, so the manager never kills a teardown inside its budget.

    Each step runs on its own: one that fails is logged by class and frames and
    the next still runs, so a broken watch loop cannot leave the queue undrained.
    A thread that still outlives the budget is named, once, at ERROR.
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
        policy = ShutdownPolicy(ctx.settings.ingest_drain_timeout_s)
        budget = ShutdownBudget(policy.total_s)
        try:
            await self._step("watch-loop stop", self._stop_watch(policy))
            await self._step("queue drain", self._drain_queue(policy, budget))
            await self._step("task drain", self._drain_tasks(budget))
        finally:
            for state in ctx.tasks.holding_threads():
                logger.error(
                    "%s task %s: scan thread still running at shutdown",
                    state.kind,
                    state.task_id,
                )

    @staticmethod
    async def _step(name: str, step: Awaitable[None]) -> None:
        """Run *step*; if it fails, log which step, by class and frames, and go on."""
        try:
            await step
        except Exception as exc:
            logger.error("teardown: %s failed", name, exc_info=RedactedFailure.of(exc))

    async def _stop_watch(self, policy: ShutdownPolicy) -> None:
        """Stop the watch loop, for no longer than its share of the budget."""
        try:
            async with asyncio.timeout(policy.watch_s):
                await self._ctx.aclose_watch_loop()
        except TimeoutError:
            logger.warning(
                "teardown: the watch loop did not stop within its %.1fs share",
                policy.watch_s,
            )

    async def _drain_queue(
        self, policy: ShutdownPolicy, budget: ShutdownBudget
    ) -> None:
        """Close and drain the queue, with at least its guaranteed share."""
        remaining = budget.remaining()
        if remaining < policy.queue_min_s:
            logger.warning(
                "teardown: %.1fs of the budget is left before the queue drain; "
                "giving it its guaranteed %.1fs",
                remaining,
                policy.queue_min_s,
            )
        await self._ctx.ingest_queue.aclose(
            drain_timeout=max(remaining, policy.queue_min_s)
        )

    async def _drain_tasks(self, budget: ShutdownBudget) -> None:
        """Drain the tracked tasks within what is left; TaskDrain logs a straggler."""
        with suppress(TimeoutError):
            await TaskDrain(self._ctx.tasks.tracked()).run(budget.remaining())
