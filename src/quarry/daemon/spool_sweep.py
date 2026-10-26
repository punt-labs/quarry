"""Sweep the spool's crash leftovers in the background, saying how it went."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, final

from quarry.daemon.daemon_threads import DaemonThreads
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.daemon.job_spool import JobSpool

logger = logging.getLogger(__name__)


@final
class StartupSweep:
    """Run :meth:`JobSpool.sweep` as a task, so startup never waits for the disk.

    The sweep takes the spool lock exclusively and removes files, so a stalled
    disk (or a writer in another daemon that holds the lock) can hold it for as
    long as it likes.  It runs on a daemon thread, off the loop, and the caller
    keeps the returned task for as long as it matters; its end, good or bad, is
    logged when it comes.
    """

    __slots__ = ()

    @classmethod
    def start(cls, spool: JobSpool) -> asyncio.Task[None]:
        """Start sweeping *spool* and return the task (the caller keeps it)."""
        task = asyncio.ensure_future(DaemonThreads.shared().run(spool.sweep))
        task.add_done_callback(cls._ended)
        return task

    @staticmethod
    def _ended(task: asyncio.Task[None]) -> None:
        """Done callback: log how the sweep ended."""
        if task.cancelled():
            logger.info("spool: the startup sweep was cancelled")
        elif (failure := task.exception()) is not None:
            logger.error(
                "spool: the startup sweep failed", exc_info=RedactedFailure.of(failure)
            )
        else:
            logger.info("spool: the startup sweep finished")
