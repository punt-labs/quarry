"""What a finalize owes one database: a rewrite when it changed, else slow upkeep."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from quarry.db.lance_connection import LanceConnection
from quarry.db.orphan_pages import OrphanPageSweep
from quarry.finalize_ledger import FinalizeLedgers
from quarry.post_sync_gc import PostSyncGc

if TYPE_CHECKING:
    import threading

    from quarry.config import Settings
    from quarry.db.table_changes import TableChanges
    from quarry.finalize_ledger import FinalizeLedger
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)

# A retry is never armed sooner: the ledger reports its back-off rounded to 0.1 s,
# and a retry that fires at once would skip and ask again in a tight loop.
MIN_RETRY_S = 1.0


@final
class TableUpkeep:
    """Decide between rewriting a table and the cheap pass, and do the cheap pass.

    Three classes of work follow a finalize.  The rewrite (compaction, the
    collection index, the version prune inside ``optimize``, the full-text
    rebuild, and the post-sync GC) is write-gated: it is owed only when the table
    changed since the last successful finalize (``TableChanges``) and the
    database is not backing off after a failure.  The orphan-page sweep is
    time-gated: once per ``TableChanges.MAINTENANCE_INTERVAL_S`` on a quiet table,
    and after every rewrite.  The capture-shadow push is unconditional and lives
    in ``TableFinalizer``.  The cheap pass writes no table and builds no index.

    Pruning old table versions is not part of the cheap pass.  LanceDB prunes only
    inside ``optimize``, which also compacts and re-indexes, and an optimize on a
    table that was just finalized still has work left to do; so the version prune
    rides the rewrite, which already passes ``cleanup_older_than`` of one hour.
    """

    __slots__ = ("_changes", "_db", "_label", "_ledger", "_lock")

    _changes: TableChanges
    _db: LanceDB
    _label: str
    _ledger: FinalizeLedger
    _lock: threading.RLock

    def __new__(cls, db: LanceDB, settings: Settings, changes: TableChanges) -> Self:
        """Bind the upkeep to a database, its write record and its failure ledger."""
        self = super().__new__(cls)
        self._changes = changes
        self._db = db
        self._label = settings.lancedb_path.parent.name
        self._ledger = FinalizeLedgers.for_database(
            settings.lancedb_path, settings.quarry_root
        )
        self._lock = LanceConnection.optimize_lock_of(db, settings.lancedb_path)
        return self

    def needs_rewrite(self) -> bool:
        """Return whether the table changed and the database is not backing off."""
        return self._changes.needs_rewrite() and not self._ledger.is_backed_off()

    def snapshot(self) -> int:
        """Return the write generation a rewrite about to start will cover."""
        return self._changes.snapshot()

    def retry_in_seconds(self) -> float | None:
        """Return seconds until a dirty table may be rewritten, or ``None`` if clean."""
        if not self._changes.needs_rewrite():
            return None
        return max(MIN_RETRY_S, self._ledger.health().retry_in_seconds)

    def begin_rewrite(self) -> bool:
        """Take the optimize lock without waiting; ``False`` if a manual run holds it.

        A finalize runs inside the global embed gate, so waiting here for a manual
        optimize that compacts for minutes would stall indexing of every other
        database.  The caller leaves the table dirty and retries later.
        """
        return self._lock.acquire(blocking=False)

    def end_rewrite(self) -> None:
        """Release the optimize lock taken by :meth:`begin_rewrite`."""
        self._lock.release()

    def settle_rewrite(self, snapshot: int, *, succeeded: bool) -> None:
        """Mark the table clean up to *snapshot* if this run's optimize succeeded.

        A failed optimize, including a failed full-text rebuild, leaves the table
        dirty, and so does a write after *snapshot*.  The orphan-page sweep
        follows either way, and the slow clock restarts only if the sweep worked.
        """
        if succeeded:
            self._changes.mark_clean(snapshot)
        self._sweep_and_mark()
        PostSyncGc().run()

    def skip_rewrite(self) -> None:
        """Log the skipped rewrite, then run the slow-clock upkeep if it is due."""
        reason = (
            "backing off after failures"
            if self._changes.needs_rewrite()
            else "table unchanged since its last finalize"
        )
        logger.debug("sync: finalize skipped for %s: %s", self._label, reason)
        if self._changes.maintenance_due():
            self._sweep_and_mark()

    def _sweep_and_mark(self) -> None:
        """Sweep orphan pages; restart the slow clock only if the sweep succeeded.

        A failed sweep leaves the clock where it was, so the next finalize tries
        again instead of waiting out ``MAINTENANCE_INTERVAL_S``.
        """
        try:
            OrphanPageSweep(self._db).run()
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("sync: orphan page sweep failed: %s", exc, exc_info=True)
            return
        self._changes.mark_maintained()
