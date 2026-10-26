"""The order a started watch loop is torn down in, and how each step is reported."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self, final

from starlette.concurrency import run_in_threadpool

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

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

        Each stage that can fail runs in the ``finally`` of the one before, so a
        failure or a cancel anywhere still cancels the timers, closes the roster
        and joins the observer.
        """
        try:
            await self._retire_safety()
        finally:
            try:
                self._attempt_all(
                    [
                        ("cancelling the submitter's retries", self._cancel_retries),
                        ("cancelling the debounce timers", self._cancel_timers),
                        ("unwatching the roster", self._roster.unwatch_all),
                    ]
                )
            finally:
                try:
                    with self._logged("closing the roster"):
                        self._roster.close()  # drop sibling conns: no leak on restart
                finally:
                    if self._source is not None:
                        await self._join_observer(self._source)

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

    @classmethod
    def _attempt_all(cls, steps: list[tuple[str, Callable[[], None]]]) -> None:
        """Run every step, logging each failure; raise the first once all have run."""
        failures = [cls._failure_of(name, call) for name, call in steps]
        for failure in failures:
            if failure is not None:
                raise failure

    @classmethod
    def _failure_of(cls, step: str, call: Callable[[], None]) -> Exception | None:
        """Run *call*; return its failure, logged once, or ``None`` if it succeeded."""
        try:
            call()
        except Exception as exc:
            logger.error("watch: %s failed", step, exc_info=RedactedFailure.of(exc))
            RedactedFailure.mark_logged(exc)
            return exc
        return None

    @staticmethod
    def _log_failure(step: str, exc: Exception) -> None:
        """Log *step*'s failure by class and frames, and note it as logged."""
        logger.error("watch: %s failed", step, exc_info=RedactedFailure.of(exc))
        RedactedFailure.mark_logged(exc)

    @classmethod
    @contextmanager
    def _logged(cls, step: str) -> Generator[None]:
        """Log *step*'s failure once, where it happens, then raise it."""
        try:
            yield
        except Exception as exc:
            cls._log_failure(step, exc)
            raise

    @classmethod
    async def _join_observer(cls, source: FsEventSource) -> None:
        """Join the observer thread off the loop; warn if a cancel leaves it running."""
        join = asyncio.ensure_future(run_in_threadpool(source.stop))
        try:
            with cls._logged("joining the observer"):
                await asyncio.shield(join)
        except asyncio.CancelledError:
            join.add_done_callback(cls._joined_late)
            logger.warning(
                "watch loop: stopped while the observer was being joined; "
                "its thread may still be running"
            )
            raise

    @staticmethod
    def _joined_late(join: asyncio.Future[None]) -> None:
        """Done callback: say how an observer join the cancel abandoned ended."""
        if join.cancelled():
            return
        failure = join.exception()  # retrieved, so asyncio never reports it unread
        if failure is not None:
            logger.error(
                "watch: the observer join failed after the stop was cancelled",
                exc_info=RedactedFailure.of(failure),
            )
        else:
            logger.info("watch: the observer joined after all")
