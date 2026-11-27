"""Post-sync finalization: rewrite a changed table, tidy a quiet one, push shadows."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.finalize_steps import ShadowPushStep
from quarry.optimize_step import OptimizeStep
from quarry.table_upkeep import TableUpkeep

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.config import Settings
    from quarry.db.table_changes import TableChanges
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)

# How long a finalize deferred by a running manual optimize waits to try again.
BUSY_RETRY_S = 30.0


@final
class TableFinalizer:
    """Rewrite a changed table, or keep an unchanged one tidy, then push shadows.

    Three classes of work.  Write-gated: the collection index, compaction, the
    version prune and the full-text rebuild (all inside ``optimize``), and the
    post-sync GC run only when the table changed since its last successful
    finalize, or this process has not finalized it yet, and the database is not
    backing off.  Time-gated: the orphan-page sweep runs on a slow clock on a quiet
    table, and after every rewrite.  Unconditional: the capture-shadow push runs on
    every finalize because it retries a deferred offline push and has no clock.
    """

    __slots__ = ("_db", "_settings", "_upkeep")

    _db: LanceDB
    _settings: Settings
    _upkeep: TableUpkeep

    def __new__(cls, db: LanceDB, settings: Settings, changes: TableChanges) -> Self:
        """Bind a database and the write record of that same connection."""
        self = super().__new__(cls)
        self._db = db
        self._settings = settings
        self._upkeep = TableUpkeep(db, settings, changes)
        return self

    def run(self, announce: Callable[[], None]) -> float | None:
        """Rewrite if the table changed, else the cheap pass; always push shadows.

        *announce* is called just before a rewrite starts.  A manual optimize that
        holds the optimize lock defers the rewrite without waiting or recording a
        failure.  Returns ``None`` when the table is clean afterwards, else the
        seconds until a retry is allowed.
        """
        busy = False
        try:
            if not self._upkeep.needs_rewrite():
                self._upkeep.skip_rewrite()
            elif self._upkeep.begin_rewrite():
                try:
                    announce()
                    self._rewrite()
                finally:
                    self._upkeep.end_rewrite()
            else:
                busy = True
                logger.debug("sync: finalize deferred: a manual optimize is running")
        finally:
            ShadowPushStep(self._settings).run()
        retry_in = self.retry_in_seconds()
        if busy and retry_in is not None:
            return max(retry_in, BUSY_RETRY_S)
        return retry_in

    def retry_in_seconds(self) -> float | None:
        """Return seconds until a dirty table may be rewritten, or ``None`` if clean."""
        return self._upkeep.retry_in_seconds()

    def _rewrite(self) -> None:
        """Optimize the table, then settle it clean only up to a prior snapshot.

        The snapshot is taken BEFORE the optimize, and the table is marked clean
        only up to it and only when this run's optimize succeeded.  A write that
        lands while this runs bumps the generation past the snapshot, so it keeps
        the table dirty for the next finalize.  The settle runs even if the
        optimize raises an unexpected exception, so the sweep and GC still happen.
        """
        snapshot = self._upkeep.snapshot()
        succeeded = False
        try:
            succeeded = OptimizeStep(self._db, self._settings).run()
        finally:
            self._upkeep.settle_rewrite(snapshot, succeeded=succeeded)
