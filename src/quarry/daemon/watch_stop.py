"""The order a started watch loop is torn down in, and how each step is reported."""

from __future__ import annotations

import asyncio
import inspect
import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from quarry.daemon.debounce import DebouncedDispatcher
    from quarry.daemon.fs_events import FsEventSource
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter

logger = logging.getLogger(__name__)

# A cleanup: what it is called in the log, and what to run.  A plain function is
# called on the loop; one that returns an awaitable is awaited.
type CleanupStep = tuple[str, Callable[[], Awaitable[None] | None]]


@final
class WatchStop:
    """Run a watch loop's cleanup steps, in order, each one whatever the others do.

    Every step runs on every exit path: when the steps before it succeed, fail, or
    are interrupted by a cancel.  A step that fails is logged once, where it
    happens, by class and frames.  A cancel always wins: it is raised after the
    last step, and a failure never replaces it.  With no cancel, the first failure
    is raised once every step has run.  A step that is still running when a cancel
    arrives is left to finish (it is shielded) and reports how it ended.
    """

    __slots__ = ("_steps",)

    _steps: tuple[CleanupStep, ...]

    def __new__(cls, steps: Iterable[CleanupStep]) -> Self:
        self = super().__new__(cls)
        self._steps = tuple(steps)
        return self

    @classmethod
    def for_loop(
        cls,
        roster: WatchRoster,
        source: FsEventSource | None,
        safety: asyncio.Task[None] | None,
        submitter: WatchSubmitter | None,
        dispatcher: DebouncedDispatcher | None,
    ) -> Self:
        """Return the teardown of a loop holding whichever collaborators exist.

        The safety task is retired first, the observer is joined last, off the
        loop, and the roster is always closed.  A collaborator the loop never
        built, or that is switched off, has no step.
        """
        steps: list[CleanupStep] = []
        if safety is not None:
            steps.append(("retiring the safety task", partial(cls.retire, safety)))
        if submitter is not None:
            steps.append(
                ("cancelling the submitter's retries", submitter.cancel_pending)
            )
        if dispatcher is not None:
            steps.append(("cancelling the debounce timers", dispatcher.cancel_all))
        steps.append(("unwatching the roster", roster.unwatch_all))
        # drop sibling conns so a restart cannot leak them
        steps.append(("closing the roster", roster.close))
        if source is not None:
            steps.append(
                ("joining the observer", partial(run_in_threadpool, source.stop))
            )
        return cls(steps)

    async def run(self) -> None:
        """Run every step; raise the cancel if there was one, else the first failure."""
        cancel: asyncio.CancelledError | None = None
        failure: Exception | None = None
        for name, step in self._steps:
            try:
                await self._attempt(name, step)
            except asyncio.CancelledError as exc:
                cancel = cancel or exc
            except Exception as exc:
                logger.error("watch: %s failed", name, exc_info=RedactedFailure.of(exc))
                RedactedFailure.mark_logged(exc)
                failure = failure or exc
        if cancel is not None:
            raise cancel
        if failure is not None:
            raise failure

    @staticmethod
    def safety_ended(task: asyncio.Task[None]) -> None:
        """Done callback: name a safety-reconcile loop that crashed, when it does."""
        if not task.cancelled() and (failure := task.exception()) is not None:
            logger.error(
                "watch: the safety reconcile loop crashed; the periodic backstop "
                "is off until the daemon restarts",
                exc_info=RedactedFailure.of(failure),
            )

    @staticmethod
    async def retire(task: asyncio.Task[None]) -> None:
        """Cancel the safety *task* and wait for it to be fully retired.

        A bare ``cancel()`` can leave "Task was destroyed but it is pending".
        ``wait`` does not raise the retired task's cancellation, so a cancel of
        THIS coroutine (the teardown's own timeout) is never mistaken for it.
        """
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "watch: the safety reconcile loop had already crashed before stop "
                "(see the earlier error)"
            )

    @classmethod
    async def _attempt(
        cls, name: str, step: Callable[[], Awaitable[None] | None]
    ) -> None:
        """Run one step; if a cancel abandons it mid-flight, say so and let it end."""
        outcome = step()
        if not inspect.isawaitable(outcome):
            return
        running = asyncio.ensure_future(outcome)
        try:
            await asyncio.shield(running)
        except asyncio.CancelledError:
            running.add_done_callback(partial(cls._ended_late, name))
            logger.warning(
                "watch: stopped while %s was in progress; it may still be running",
                name,
            )
            raise

    @staticmethod
    def _ended_late(name: str, running: asyncio.Future[None]) -> None:
        """Done callback: say how a step the cancel abandoned ended."""
        if running.cancelled():
            return
        failure = running.exception()  # retrieved, so asyncio never reports it unread
        if failure is None:
            logger.info("watch: %s finished after the stop was cancelled", name)
        else:
            logger.error(
                "watch: %s failed after the stop was cancelled",
                name,
                exc_info=RedactedFailure.of(failure),
            )
