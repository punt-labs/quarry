"""Remove the spool record of a job that stored after all."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from quarry.private_write import PrivateWrite
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.abort_reason import Aborted
    from quarry.daemon.daemon_threads import DaemonThreads

logger = logging.getLogger(__name__)


@final
class SpoolDiscard:
    """Delete a spool record on a daemon thread, durably, best effort.

    A job the abort spooled and that then stored must not leave its record
    behind: running the record again would store it twice.  Removal syncs the
    directory, so it runs off the loop; a failure, or a removal the loop's close
    cancelled, is logged by file name and nothing more (a crash between the store
    and the removal is the one case it cannot cover).  A teardown that must not
    leave a removal undone waits for :meth:`drain`.
    """

    __slots__ = ("_removals", "_threads")

    _removals: dict[asyncio.Future[None], Path]
    _threads: DaemonThreads

    def __new__(cls, threads: DaemonThreads) -> Self:
        self = super().__new__(cls)
        self._removals = {}
        self._threads = threads
        return self

    @property
    def pending(self) -> tuple[Path, ...]:
        """Return the records whose removal has not ended."""
        return tuple(self._removals.values())

    def discard(self, path: Path) -> None:
        """Remove *path*, off the loop."""
        removal = asyncio.ensure_future(self._threads.run(partial(self._unlink, path)))
        self._removals[removal] = path
        removal.add_done_callback(self._ended)

    def discard_late(self, spooling: asyncio.Future[Aborted]) -> None:
        """Remove the record a late spool wrote, once it has."""
        if (
            not spooling.cancelled()
            and spooling.exception() is None
            and (path := spooling.result().path) is not None
        ):
            self.discard(path)

    async def drain(self, timeout: float) -> tuple[Path, ...]:
        """Wait up to *timeout* seconds for the removals; return those still running."""
        if self._removals:
            await asyncio.wait(set(self._removals), timeout=timeout)
        return self.pending

    def _ended(self, removal: asyncio.Future[None]) -> None:
        """Done callback: say how a removal ended, if it did not go well."""
        path = self._removals.pop(removal)
        if removal.cancelled():
            logger.warning(
                "abort: removing the spool record %s was cancelled; "
                "it may still be on disk",
                path.name,
            )
        elif (failure := removal.exception()) is not None:
            logger.error(
                "abort: removing the spool record %s failed",
                path.name,
                exc_info=RedactedFailure.of(failure),
            )

    @staticmethod
    def _unlink(path: Path) -> None:
        """Remove *path* and sync its directory; a failure is logged, never raised."""
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "abort: could not remove the spool record of a job that stored",
                exc_info=RedactedFailure.of(exc),
            )
            return
        PrivateWrite.sync_directory(path.parent, f"spool record {path.name} removed")
