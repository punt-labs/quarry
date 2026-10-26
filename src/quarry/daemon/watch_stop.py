"""The order a started watch loop is torn down in, and how each step is reported."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.debounce import DebouncedDispatcher
    from quarry.daemon.fs_events import FsEventSource
    from quarry.daemon.watch_roster import WatchRoster
    from quarry.daemon.watch_submit import WatchSubmitter

logger = logging.getLogger(__name__)


@final
class WatchStop:
    """Tear down one started watch loop: the safety task, the roster, the observer.

    Safe to cancel.  Whatever a cancel interrupts, the roster is closed; only the
    observer's join can outlive a cancel, and it is shielded and says so.  Each
    cleanup that fails is logged where it happens, by class and frames, then
    raised.  A crash of the safety-reconcile loop is named when it happens
    (:meth:`safety_ended`, attached at creation), and noted again here.
    """

    __slots__ = ("_dispatcher", "_roster", "_safety", "_source", "_submitter")

    _dispatcher: DebouncedDispatcher | None  # None: never built (a loop not started)
    _roster: WatchRoster
    _safety: asyncio.Task[None] | None  # None: the periodic backstop is off
    _source: FsEventSource | None  # None: no observer was built
    _submitter: WatchSubmitter | None  # None: never built

    def __new__(
        cls,
        roster: WatchRoster,
        source: FsEventSource | None,
        safety: asyncio.Task[None] | None,
        submitter: WatchSubmitter | None,
        dispatcher: DebouncedDispatcher | None,
    ) -> Self:
        self = super().__new__(cls)
        self._roster = roster
        self._source = source
        self._safety = safety
        self._submitter = submitter
        self._dispatcher = dispatcher
        return self

    async def run(self) -> None:
        """Retire the safety task, cancel timers, close the roster, join the observer.

        Every cleanup runs and logs its own failure whatever happens before it.  A
        cancel that arrives while the safety task retires is what propagates: the
        cleanups still run, and a failure among them is logged and never replaces
        it.  With no cancel, the first failure is raised once all have run.
        """
        try:
            await self._retire_safety()
        except asyncio.CancelledError:
            await self._finish()  # the cancel wins; failures here are logged only
            raise
        failure = await self._finish()
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

    async def _retire_safety(self) -> None:
        """Cancel the safety task and wait for it to be fully retired.

        A bare ``cancel()`` can leave "Task was destroyed but it is pending".
        ``wait`` does not raise the retired task's cancellation, so a cancel of
        THIS coroutine (the teardown's own timeout) is never mistaken for it.
        """
        if self._safety is None:
            return
        self._safety.cancel()
        await asyncio.wait({self._safety})
        if not self._safety.cancelled() and self._safety.exception() is not None:
            logger.error(
                "watch: the safety reconcile loop had already crashed before stop "
                "(see the earlier error)"
            )

    def _cancel_retries(self) -> None:
        """Cancel outstanding backoff re-arms, if there is a submitter."""
        if self._submitter is not None:
            self._submitter.cancel_pending()

    def _cancel_timers(self) -> None:
        """Cancel every debounce timer and refuse more, if there is a dispatcher."""
        if self._dispatcher is not None:
            self._dispatcher.cancel_all()

    async def _finish(self) -> Exception | None:
        """Run every cleanup; return the first failure (each already logged)."""
        failures = [
            self._failure_of(name, call)
            for name, call in (
                ("cancelling the submitter's retries", self._cancel_retries),
                ("cancelling the debounce timers", self._cancel_timers),
                ("unwatching the roster", self._roster.unwatch_all),
                # drop sibling conns so a restart cannot leak them
                ("closing the roster", self._roster.close),
            )
        ]
        if self._source is not None:
            failures.append(await self._join_observer(self._source))
        return next((failure for failure in failures if failure is not None), None)

    @staticmethod
    def _failure_of(step: str, call: Callable[[], None]) -> Exception | None:
        """Run *call*; return its failure, logged once, or ``None`` if it succeeded.

        The one place a cleanup failure is logged (by class and frames) and marked
        so a later boundary does not log it again.
        """
        try:
            call()
        except Exception as exc:
            logger.error("watch: %s failed", step, exc_info=RedactedFailure.of(exc))
            RedactedFailure.mark_logged(exc)
            return exc
        return None

    @classmethod
    async def _join_observer(cls, source: FsEventSource) -> Exception | None:
        """Join the observer thread off the loop; warn if a cancel leaves it running.

        Returns the join's failure (logged from the joining thread), if any.
        """
        join = asyncio.ensure_future(
            run_in_threadpool(cls._failure_of, "joining the observer", source.stop)
        )
        try:
            return await asyncio.shield(join)
        except asyncio.CancelledError:
            join.add_done_callback(cls._joined_late)
            logger.warning(
                "watch loop: stopped while the observer was being joined; "
                "its thread may still be running"
            )
            raise

    @staticmethod
    def _joined_late(join: asyncio.Future[Exception | None]) -> None:
        """Done callback: say how an observer join the cancel abandoned ended."""
        if join.cancelled():
            return
        failure = join.result()  # retrieved, so asyncio never reports it unread
        if failure is None:
            logger.info("watch: the observer joined after all")
        else:
            logger.warning(
                "watch: the observer join failed after the stop was cancelled "
                "(logged when it failed)"
            )
