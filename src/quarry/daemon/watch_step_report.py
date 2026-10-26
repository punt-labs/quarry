"""Say how a watch-stop step that did not end with the stop ended."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, final

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)


@final
class StepReport:
    """The words for a cleanup step the stop left running, or that ended with it.

    A step is one of: still running when the stop gave up on it; queued behind a
    stuck step and never started (the serial thread pool runs one at a time); or
    started with no grace left and not awaited.  One that ended in the same tick
    as the cancel is reported as it ended, not as abandoned.
    """

    __slots__ = ()

    @classmethod
    def abandon(
        cls,
        name: str,
        running: asyncio.Future[None],
        # None before any cancel: the step was given as long as it takes.
        remaining: float | None,
        # The step it waits behind, if it has not begun; None if it has.
        queued_behind: str | None = None,
    ) -> None:
        """Leave *running* to finish on its own, saying so now and when it ends.

        One that finished in the same tick as the cancel is not abandoned: it is
        reported as it ended.
        """
        if running.done():
            cls._ended_with_the_cancel(name, running)
            return
        running.add_done_callback(partial(cls._ended_late, name))
        if queued_behind is not None:
            logger.warning(
                "watch: %s never started; queued behind %s", name, queued_behind
            )
        elif remaining is not None and remaining <= 0:
            logger.warning(
                "watch: %s was started after the grace ran out and is not awaited",
                name,
            )
        else:
            logger.warning(
                "watch: stopped while %s was in progress; it may still be running",
                name,
            )

    @staticmethod
    def _ended_with_the_cancel(name: str, running: asyncio.Future[None]) -> None:
        """Report a step that ended with the cancel; a clean end says nothing."""
        if running.cancelled():
            logger.warning("watch: %s was cancelled as the stop was cancelled", name)
        elif (failure := running.exception()) is not None:
            logger.error(
                "watch: %s failed as the stop was cancelled",
                name,
                exc_info=RedactedFailure.of(failure),
            )

    @staticmethod
    def _ended_late(name: str, running: asyncio.Future[None]) -> None:
        """Done callback: say how a step the stop gave up on ended."""
        if running.cancelled():
            logger.warning("watch: %s was cancelled after the stop gave up on it", name)
        elif (failure := running.exception()) is not None:
            logger.error(
                "watch: %s failed after the stop gave up on it",
                name,
                exc_info=RedactedFailure.of(failure),
            )
        else:
            logger.info("watch: %s finished after the stop gave up on it", name)


class StepCancelledError(Exception):
    """A cleanup step was cancelled by something other than the teardown's cancel."""
