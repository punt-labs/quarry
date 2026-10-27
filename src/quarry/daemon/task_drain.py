"""Await a set of background tasks for real, and give up on one that will not cancel."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar, Self, final

from quarry.shutdown_policy import ShutdownPolicy

logger = logging.getLogger(__name__)


@final
class TaskDrain:
    """Wait for tasks to complete, then cancel the stragglers, within one budget.

    Cancelling the wrapping ``asyncio.Task`` does not stop a
    ``run_in_threadpool`` job's underlying OS thread, so a cancel-only teardown
    lets that thread keep executing after the caller believes the task gone.
    Waiting for genuine completion closes that race.

    The *timeout* is the whole budget and is never exceeded: the cancel wait sits
    inside it, as the last :attr:`~ShutdownPolicy.CANCEL_SHARE` of it, and never less
    than :attr:`FLOOR_S`, so tasks that finish just after the queue abort are not
    cancelled.  A task that outlives the
    rest is a straggler: its wrapper is cancelled, and the drain fails closed
    (``TimeoutError``) rather than let the caller believe it succeeded.  Every
    tracked task is drained, whatever its kind: a sync or an ingest task left
    running past shutdown writes after the queue it submits to is gone, which a
    backfill is only the longest example of.
    """

    # The least a drain waits, whatever the budget has left (the margin pays it).
    FLOOR_S: ClassVar[float] = ShutdownPolicy.TASK_FLOOR_S

    __slots__ = ("_tasks",)

    _tasks: list[asyncio.Task[None]]

    def __new__(cls, tasks: list[asyncio.Task[None]]) -> Self:
        self = super().__new__(cls)
        self._tasks = tasks
        return self

    async def run(self, timeout: float) -> None:
        """Await every task's completion within *timeout*, else cancel and raise."""
        if not self._tasks:
            return
        timeout = max(timeout, self.FLOOR_S)
        cancel_wait = ShutdownPolicy.cancel_s(timeout)
        _, pending = await asyncio.wait(self._tasks, timeout=timeout - cancel_wait)
        if pending:
            await self._cancel_stragglers(pending, cancel_wait)
            logger.error(
                "task drain: %d task(s) did not complete within %.1fs; their "
                "wrapper was cancelled but the underlying thread may still be running",
                len(pending),
                timeout,
            )
            msg = "background tasks did not drain"
            raise TimeoutError(msg)

    @staticmethod
    async def _cancel_stragglers(
        stragglers: set[asyncio.Task[None]], wait_s: float
    ) -> None:
        """Cancel *stragglers* and wait for them for *wait_s*, no longer."""
        for task in stragglers:
            task.cancel()
        _, pending = await asyncio.wait(stragglers, timeout=wait_s)
        if pending:
            logger.error(
                "task drain: %d task(s) did not stop within %.1fs of the cancel",
                len(pending),
                wait_s,
            )
