"""What a finalize owes one database: a rewrite when it changed, else slow upkeep."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.db.orphan_pages import OrphanPageSweep
from quarry.db.table_changes import TableChanges
from quarry.finalize_ledger import FinalizeLedgers
from quarry.post_sync_gc import PostSyncGc

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.finalize_ledger import FinalizeLedger
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@final
class TableUpkeep:
    """Decide between rewriting a table and the cheap pass, and do the cheap pass.

    The rewrite (compaction, the collection index and the full-text rebuild) is
    gated on writes: it is owed only when the table changed since the last
    successful finalize (``TableChanges``).  The cheap pass is gated on the clock:
    sweeping orphan pages (a 24 hour grace) is owed on a quiet database too, once
    per ``TableChanges.MAINTENANCE_INTERVAL_S``.  Neither rewrites a table or
    rebuilds an index.

    Pruning old table versions is not part of the cheap pass.  LanceDB prunes only
    inside ``optimize``, which also compacts and re-indexes, and an optimize on a
    table that was just finalized still has work left to do; so the version prune
    rides the rewrite, which already passes ``cleanup_older_than`` of one hour.
    """

    __slots__ = ("_changes", "_db", "_label", "_ledger")

    _changes: TableChanges
    _db: LanceDB
    _label: str
    _ledger: FinalizeLedger

    def __new__(cls, db: LanceDB, settings: Settings) -> Self:
        """Bind the upkeep to a database, its write record and its failure ledger."""
        self = super().__new__(cls)
        self._changes = TableChanges.for_path(settings.lancedb_path)
        self._db = db
        self._label = settings.lancedb_path.parent.name
        self._ledger = FinalizeLedgers.for_database(
            settings.lancedb_path, settings.quarry_root
        )
        return self

    def needs_rewrite(self) -> bool:
        """Return whether the table changed since the last successful finalize."""
        return self._changes.needs_rewrite()

    def snapshot(self) -> int:
        """Return the write generation a rewrite about to start will cover."""
        return self._changes.snapshot()

    def settle_rewrite(self, snapshot: int) -> None:
        """Mark the table clean up to *snapshot* if its optimize left no failure.

        The optimize step records any failure it hit, a refused compaction or a
        back-off in the database's ledger and clears the ledger when it
        succeeds, so a ledger with no failures means the table is rewritten.  A
        write after *snapshot* keeps the table dirty either way.  The orphan-page
        sweep follows, and the slow clock restarts.
        """
        if self._ledger.health().consecutive_failures == 0:
            self._changes.mark_clean(snapshot)
        self._sweep_pages()
        self._changes.mark_maintained()
        PostSyncGc().run()

    def skip_rewrite(self) -> None:
        """Log the skipped rewrite, then run the slow-clock upkeep if it is due."""
        logger.debug(
            "sync: finalize skipped for %s: table unchanged since its last finalize",
            self._label,
        )
        if self._changes.maintenance_due():
            self._sweep_pages()
            self._changes.mark_maintained()

    def _sweep_pages(self) -> None:
        try:
            OrphanPageSweep(self._db).run()
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("sync: orphan page sweep failed: %s", exc, exc_info=True)
