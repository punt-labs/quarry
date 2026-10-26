"""The order a started watch loop is torn down in, and how each step is reported."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.redacted_failure import RedactedFailure
from quarry.shutdown_policy import ShutdownPolicy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from quarry.daemon.debounce import DebouncedDispatcher
    from quarry.daemon.fs_events import FsEventSource
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter

logger = logging.getLogger(__name__)

# A cleanup: what it is called in the log, and what to run.
type CleanupStep = tuple[str, Callable[[], Awaitable[None]]]


@final
class WatchStop:
    """Run a watch loop's cleanup steps, in order, each one whatever the others do.

    Every step runs on every exit path: when the steps before it succeed, fail, or
    are interrupted by a cancel or by anything else.  A step that fails is logged
    once, where it happens, by class and frames.

    Once a cancel is seen (the teardown's own timeout), the remaining steps share
    a bounded grace period, so the stop overruns its share by no more than that.
    A step that does not finish inside it, or that the cancel interrupted, is left
    running, named in a warning, and says how it ended when it does.  Steps still
    start in order, each after the previous finished or was given up on.

    What is raised once every step has run, strongest first: a ``BaseException``
    that is not a cancel, the cancel, the first failure.  The one limit is that
    asyncio ends the whole event loop on a ``KeyboardInterrupt`` or ``SystemExit``
    raised inside a task, so a step that raises one cannot be survived here.
    """

    __slots__ = ("_grace_s", "_steps")

    _grace_s: float
    _steps: tuple[CleanupStep, ...]

    def __new__(cls, steps: Iterable[CleanupStep], grace_s: float) -> Self:
        self = super().__new__(cls)
        self._steps = tuple(steps)
        self._grace_s = grace_s
        return self

    @classmethod
    def for_loop(
        cls,
        roster: WatchRoster,
        source: FsEventSource | None,
        safety: asyncio.Task[None] | None,
        submitter: WatchSubmitter | None,
        dispatcher: DebouncedDispatcher | None,
        drain_timeout_s: float,
    ) -> Self:
        """Return the teardown of a loop holding whichever collaborators exist.

        The safety task is retired first, the observer is joined last, off the
        loop, and the roster is always closed.  A collaborator the loop never
        built, or that is switched off, has no step.  *drain_timeout_s* is the
        teardown budget the grace after a cancel is a share of.
        """
        steps: list[CleanupStep] = []
        if safety is not None:
            steps.append(("retiring the safety task", partial(cls.retire, safety)))
        if submitter is not None:
            steps.append(
                (
                    "cancelling the submitter's retries",
                    cls.inline(submitter.cancel_pending),
                )
            )
        if dispatcher is not None:
            steps.append(
                ("cancelling the debounce timers", cls.inline(dispatcher.cancel_all))
            )
        steps.append(("unwatching the roster", cls.inline(roster.unwatch_all)))
        # drop sibling conns so a restart cannot leak them
        steps.append(("closing the roster", cls.inline(roster.close)))
        if source is not None:
            steps.append(
                ("joining the observer", partial(run_in_threadpool, source.stop))
            )
        return cls(steps, ShutdownPolicy(drain_timeout_s).watch_grace_s)

    @staticmethod
    def inline(call: Callable[[], None]) -> Callable[[], Awaitable[None]]:
        """Return *call* as a step: it runs on the loop, to completion."""

        async def step() -> None:
            call()

        return step

    async def run(self) -> None:
        """Run every step, then raise what the strongest outcome calls for."""
        outcome = _Outcome()
        loop = asyncio.get_running_loop()
        deadline: float | None = None
        for name, step in self._steps:
            remaining = None if deadline is None else max(deadline - loop.time(), 0.0)
            try:
                await self._attempt(name, step, remaining)
            except asyncio.CancelledError as exc:
                outcome.cancelled(exc)
                if deadline is None:
                    deadline = loop.time() + self._grace_s
            except BaseException as exc:
                logger.error("watch: %s failed", name, exc_info=RedactedFailure.of(exc))
                RedactedFailure.mark_logged(exc)
                outcome.failed(exc)
        outcome.raise_strongest()

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
        cls, name: str, step: Callable[[], Awaitable[None]], remaining: float | None
    ) -> None:
        """Run one step for at most *remaining* seconds (``None``: as long as it takes).

        A step the cancel interrupts, or that outlives *remaining*, is left
        running with a callback that reports how it ends, and is named in a
        warning; an interrupted one re-raises the cancel.
        """
        running = asyncio.ensure_future(step())
        try:
            done, _ = await asyncio.wait({running}, timeout=remaining)
        except asyncio.CancelledError:
            cls._abandon(name, running)
            raise
        if running in done:
            running.result()  # its failure, if any, propagates to be logged once
        else:
            cls._abandon(name, running)

    @classmethod
    def _abandon(cls, name: str, running: asyncio.Future[None]) -> None:
        """Leave *running* to finish on its own, saying so now and when it ends."""
        running.add_done_callback(partial(cls._ended_late, name))
        logger.warning(
            "watch: stopped while %s was in progress; it may still be running", name
        )

    @staticmethod
    def _ended_late(name: str, running: asyncio.Future[None]) -> None:
        """Done callback: say how a step the stop gave up on ended."""
        if running.cancelled():
            return
        failure = running.exception()  # retrieved, so asyncio never reports it unread
        if failure is None:
            logger.info("watch: %s finished after the stop gave up on it", name)
        else:
            logger.error(
                "watch: %s failed after the stop gave up on it",
                name,
                exc_info=RedactedFailure.of(failure),
            )


@final
class _Outcome:
    """What the steps of one stop ended in, and which of it is raised.

    Strongest first: a ``BaseException`` that is not a cancel, the cancel, the
    first failure.
    """

    __slots__ = ("_cancel", "_failure", "_fatal")

    _cancel: asyncio.CancelledError | None
    _failure: Exception | None
    _fatal: BaseException | None

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._cancel = self._failure = self._fatal = None
        return self

    def cancelled(self, exc: asyncio.CancelledError) -> None:
        """Remember the first cancel."""
        self._cancel = self._cancel or exc

    def failed(self, exc: BaseException) -> None:
        """Remember the first failure of its kind."""
        if isinstance(exc, Exception):
            self._failure = self._failure or exc
        else:
            self._fatal = self._fatal or exc

    def raise_strongest(self) -> None:
        """Raise the strongest thing remembered, if anything."""
        for found in (self._fatal, self._cancel, self._failure):
            if found is not None:
                raise found
