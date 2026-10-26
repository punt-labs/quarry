"""Remove the spool record of a job that stored after all."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING, Self, final

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.daemon.abort_reason import Aborted
    from quarry.daemon.daemon_threads import DaemonThreads

logger = logging.getLogger(__name__)


@final
class SpoolDiscard:
    """Delete a spool record on a daemon thread, best effort.

    A job the abort spooled and that then stored must not leave its record
    behind: running the record again would store it twice.  Removal syncs a
    directory, so it runs off the loop; a failure is logged and nothing more (a
    crash between the store and the removal is the one case it cannot cover).
    """

    __slots__ = ("_removals", "_threads")

    _removals: set[asyncio.Future[None]]
    _threads: DaemonThreads

    def __new__(cls, threads: DaemonThreads) -> Self:
        self = super().__new__(cls)
        self._removals = set()
        self._threads = threads
        return self

    def discard(self, path: Path | None) -> None:
        """Remove *path*, if there is one."""
        if path is None:
            return
        removal = asyncio.ensure_future(self._threads.run(partial(self._unlink, path)))
        self._removals.add(removal)
        removal.add_done_callback(self._removals.discard)

    def discard_late(self, spooling: asyncio.Future[Aborted]) -> None:
        """Remove the record a late spool wrote, once it has."""
        if not spooling.cancelled() and spooling.exception() is None:
            self.discard(spooling.result().path)

    @staticmethod
    def _unlink(path: Path) -> None:
        """Remove *path*; a failure is logged, never raised."""
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "abort: could not remove the spool record of a job that stored",
                exc_info=RedactedFailure.of(exc),
            )
