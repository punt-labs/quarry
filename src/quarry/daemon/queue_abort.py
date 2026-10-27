"""Abort a queue's workers after a drain outran its window, and say what it cost."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.abort_tally import AbortTally
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.daemon.collection_worker import CollectionWorker

logger = logging.getLogger(__name__)


@final
class QueueAbort:
    """Abort *workers*, wait for them at most *wait_s*, and report the cost.

    The summary's terms always add up: jobs whose spool write has not ended are
    counted as "not yet accounted for", and every worker that outlives the wait
    reports how it ended when it does.
    """

    __slots__ = ("_wait_s", "_workers")

    _wait_s: float
    _workers: list[CollectionWorker]

    def __new__(cls, workers: list[CollectionWorker], wait_s: float) -> Self:
        self = super().__new__(cls)
        self._workers = workers
        self._wait_s = wait_s
        return self

    async def run(self, window_s: float) -> None:
        """Abort the workers of a drain that did not finish within *window_s*."""
        tasks = [worker.abort_off_loop() for worker in self._workers]
        done, pending = await asyncio.wait(tasks, timeout=self._wait_s)
        for task in done:
            # A worker that died of anything but the cancel would otherwise end as
            # "exception never retrieved", never named.
            if not task.cancelled() and (failure := task.exception()) is not None:
                logger.error(
                    "ingest queue: a worker task failed while aborting",
                    exc_info=RedactedFailure.of(failure),
                )
        for worker, task in zip(self._workers, tasks, strict=True):
            if task in pending:
                task.add_done_callback(partial(self._ended_late, worker))
        logger.warning(
            "ingest queue: drain did not finish within %.1fs; aborted %s",
            window_s,
            self._summary(len(pending)),
        )

    def _summary(self, stragglers: int) -> str:
        """Return the abort's cost in words; its terms add up to the job count."""
        parts = [AbortTally.total(w.tally for w in self._workers).summary()]
        if unaccounted := sum(w.pending_jobs for w in self._workers):
            parts.append(f"{unaccounted} job(s) not yet accounted for")
        if stragglers:
            parts.append(
                f"{stragglers} worker(s) did not stop within {self._wait_s:g}s"
            )
        return "; ".join(parts)

    @staticmethod
    def _ended_late(worker: CollectionWorker, task: asyncio.Task[None]) -> None:
        """Done callback: say how a worker the abort stopped waiting for ended.

        Its tally and the jobs it had not yet accounted for are reported then, so
        a job the first summary called "not yet accounted for" is accounted for
        in the log once it is.  A cancel is the expected ending; a failure is not.
        """
        outcome = f"{worker.tally.summary()}; {worker.pending_jobs} job(s) pending"
        if task.cancelled():
            logger.info(
                "ingest queue: a worker that outlived the abort wait was cancelled: %s",
                outcome,
            )
        elif (failure := task.exception()) is not None:
            logger.error(
                "ingest queue: a worker that outlived the abort wait failed: %s",
                outcome,
                exc_info=RedactedFailure.of(failure),
            )
        else:
            logger.info(
                "ingest queue: a worker that outlived the abort wait ended: %s",
                outcome,
            )
