"""What an aborted queue does before its loop closes: wait, tally, report."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.abort_tally import AbortTally

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.collection_worker import CollectionWorker
    from quarry.daemon.store_run import StoreRun

logger = logging.getLogger(__name__)


@final
class LoopClose:
    """Settle an aborted queue's late stores and removals, then report the rest.

    The observer of a store, and the removal of a spool record, are callbacks on
    the event loop, and the loop does not outlive every thread: a store whose
    worker was cancelled can still be inserting when ``asyncio.run`` closes it.
    So before the queue's close returns, this waits (never joins) for the stores
    whose end nobody has read and for the removals under way, up to a deadline;
    then it logs one corrected tally, after the moves those waits allowed, and a
    WARNING for everything it could not wait out, by kind, collection and task.
    Nothing it cannot wait for is silent.
    """

    __slots__ = ("_workers",)

    _workers: list[CollectionWorker]

    def __new__(cls, workers: list[CollectionWorker]) -> Self:
        self = super().__new__(cls)
        self._workers = workers
        return self

    async def run(self, deadline: float) -> None:
        """Wait until nothing is running or the loop clock reaches *deadline*."""
        stores = [s for w in self._workers for s in w.unobserved_stores()]
        await self._wait_for(stores, deadline)
        removing = await self._drain(deadline)
        stragglers = [s for w in self._workers for s in w.straggling_stores()]
        self._report([s for s in stores if s.is_unobserved] + stragglers, removing)

    @staticmethod
    async def _wait_for(stores: list[StoreRun], deadline: float) -> None:
        """Wait, up to *deadline*, for each store's end to be read; cancel no store."""
        waits = [asyncio.ensure_future(store.observed()) for store in stores]
        if waits:
            remaining = max(deadline - asyncio.get_running_loop().time(), 0.0)
            await asyncio.wait(waits, timeout=remaining)
        for wait in waits:
            wait.cancel()  # the wait, never the store

    async def _drain(self, deadline: float) -> list[Path]:
        """Drain every worker's spool removals; return those still running."""
        loop = asyncio.get_running_loop()
        removing: list[Path] = []
        for worker in self._workers:
            removing += await worker.drain_removals(max(deadline - loop.time(), 0.0))
        return removing

    def _report(self, stores: list[StoreRun], removing: list[Path]) -> None:
        """Log the corrected tally, then every store and removal still running."""
        total = AbortTally.total(w.tally for w in self._workers)
        logger.warning("ingest queue: after the drain, %s", total.summary())
        for store in stores:
            logger.warning(
                "%s: store still running at shutdown; its spool record "
                "(if any) may duplicate stored content",
                store.describe(),
            )
        for path in removing:
            logger.warning(
                "abort: removing the spool record %s was still running at "
                "shutdown; it may stay on disk",
                path.name,
            )
