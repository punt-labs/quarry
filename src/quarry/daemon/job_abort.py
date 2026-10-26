"""Fail the jobs a drain-timeout abort cuts off, spooling those that need it."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.abort_reason import AbortReason

if TYPE_CHECKING:
    from collections.abc import Sequence

    from quarry.config import Settings
    from quarry.daemon.abort_reason import Aborted, AbortTally
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState


@final
@dataclass(frozen=True, slots=True)
class QueuedJob:
    """One admitted job paired with the task state it reports through."""

    job: IngestUnit
    state: TaskState


@final
class JobAbort:
    """Fail aborted jobs with their truthful reason, and count how each ended.

    Deciding how a job ends can spool it, and spooling syncs files, so there are
    two ways in: :meth:`fail` runs the syncs in the caller, and the ``async``
    ones run them on a thread so one worker's abort never stalls the event loop
    for the others.  State and the tally are only ever touched on the caller's
    thread (or the loop), after the syncs are done.
    """

    __slots__ = ("_reason", "_tally")

    _reason: AbortReason
    _tally: AbortTally

    def __new__(cls, settings: Settings, tally: AbortTally) -> Self:
        self = super().__new__(cls)
        self._reason = AbortReason.for_settings(settings)
        self._tally = tally
        return self

    def fail(self, items: Sequence[QueuedJob]) -> None:
        """Spool and fail *items* here and now (a synchronous teardown)."""
        self._record(items, self._outcomes(items))

    async def fail_off_loop(self, items: Sequence[QueuedJob]) -> None:
        """Spool *items* on a thread, then fail them."""
        self._record(items, await asyncio.to_thread(self._outcomes, items))

    async def fail_in_flight(self, item: QueuedJob) -> None:
        """Spool and fail the in-flight *item*; a repeated cancel never skips it.

        The drain timeout cancels the worker, then the abort cancels it again
        while it is still spooling, so the wait is re-entered until the spool
        thread is done and the job is recorded.  The thread is bounded (a few
        syncs).
        """
        spooling = asyncio.ensure_future(asyncio.to_thread(self._outcomes, [item]))
        while True:
            try:
                [outcome] = await asyncio.shield(spooling)
                break
            except asyncio.CancelledError:
                if spooling.done():
                    [outcome] = spooling.result()
                    break
        self._record([item], [outcome])

    def _outcomes(self, items: Sequence[QueuedJob]) -> list[Aborted]:
        """Return how each of *items* ends, spooling those that need it (blocking)."""
        return [self._reason.of(item.job) for item in items]

    def _record(self, items: Sequence[QueuedJob], outcomes: list[Aborted]) -> None:
        """Fail each of *items* with its reason and count it."""
        for item, aborted in zip(items, outcomes, strict=True):
            item.state.status = "failed"
            item.state.error = aborted.reason
            self._tally.record(aborted)
