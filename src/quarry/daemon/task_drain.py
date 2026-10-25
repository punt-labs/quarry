"""Await a set of background tasks for real, and give up on one that will not cancel."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar, Self, final

logger = logging.getLogger(__name__)


@final
class TaskDrain:
    """Wait for tasks to complete, then cancel the stragglers, each wait bounded.

    Cancelling the wrapping ``asyncio.Task`` does not stop a
    ``run_in_threadpool`` job's underlying OS thread, so a cancel-only teardown
    lets that thread keep executing after the caller believes the task gone.
    Waiting for genuine completion closes that race.

    A task that outlives the drain window is a straggler: its wrapper is
    cancelled, and the drain fails closed (``TimeoutError``) rather than let the
    caller believe it succeeded.  Cancelling a backfill waits for its scan thread
    to return, which its own bound limits to :attr:`CANCEL_WAIT_S` less a margin,
    so the wait after a cancel is bounded by that figure too: a shutdown takes no
    longer than the drain window plus :attr:`CANCEL_WAIT_S`.
    """

    # The scan's bound on its wait for its thread after a stop is 30 s
    # (``BackfillTask.STOP_BOUND_S``); one more second covers its poll.
    CANCEL_WAIT_S: ClassVar[float] = 31.0

    __slots__ = ("_cancel_wait_s", "_tasks")

    _cancel_wait_s: float
    _tasks: list[asyncio.Task[None]]

    def __new__(
        cls, tasks: list[asyncio.Task[None]], cancel_wait_s: float = CANCEL_WAIT_S
    ) -> Self:
        self = super().__new__(cls)
        self._tasks = tasks
        self._cancel_wait_s = cancel_wait_s
        return self

    async def run(self, timeout: float) -> None:
        """Await every task's completion within *timeout*, else cancel and raise."""
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=timeout)
        if pending:
            await self._cancel_stragglers(pending)
            logger.error(
                "task drain: %d task(s) did not complete within %.1fs; their "
                "wrapper was cancelled but the underlying thread may still be running",
                len(pending),
                timeout,
            )
            msg = "background tasks did not drain"
            raise TimeoutError(msg)

    async def _cancel_stragglers(self, stragglers: set[asyncio.Task[None]]) -> None:
        """Cancel *stragglers* and wait for them, but not past the cancel bound."""
        for task in stragglers:
            task.cancel()
        _, pending = await asyncio.wait(stragglers, timeout=self._cancel_wait_s)
        if pending:
            logger.error(
                "task drain: %d task(s) did not stop within %.1fs of the cancel",
                len(pending),
                self._cancel_wait_s,
            )
