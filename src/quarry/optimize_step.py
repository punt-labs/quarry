"""The optimize steps: the finalize's, with back-off, and the operator's."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Self, final

from quarry.db import TableOptimizer
from quarry.db.table_changes import TableChanges
from quarry.finalize_ledger import FinalizeLedger, FinalizeLedgers

if TYPE_CHECKING:
    import threading

    from quarry.config import Settings
    from quarry.db.optimizer import OptimizeOutcome
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)

_FTS_FAILED = "full-text index rebuild failed; the next finalize retries it"


@final
class OptimizeStep:
    """Optimize one database unless backed off; a failure is recorded, not raised.

    Cleanup shares ``table.optimize()`` with compaction, so a failing optimize
    leaks orphaned files on every attempt.  The failure must not stop the rest of
    the finalize, and the ledger spaces the retries out.  The ledger carries only
    the exception type: its snapshot is served unauthenticated on ``/health``,
    while the full error goes to the log.
    """

    __slots__ = ("_db", "_ledger", "_lock")

    _db: LanceDB
    _ledger: FinalizeLedger
    _lock: threading.RLock

    def __new__(cls, db: LanceDB, settings: Settings) -> Self:
        """Bind optimization to a database and its shared failure-backoff ledger."""
        self = super().__new__(cls)
        self._db = db
        self._ledger = FinalizeLedgers.for_database(
            settings.lancedb_path, settings.quarry_root
        )
        self._lock = TableChanges.for_path(settings.lancedb_path).optimizing()
        return self

    def run(self) -> bool:
        """Rebuild indexes and compact; return whether the table is now optimized.

        ``True`` only when the compaction ran, the full-text rebuild succeeded and
        the collection index was built (a database with no table has nothing to
        do).  ``False`` means the table still needs the work: the step was skipped
        while backing off, it failed, compaction was refused, or the full-text
        rebuild failed and left the old index stale.
        """
        with self._lock:
            if self._ledger.is_backed_off():
                logger.warning("sync: optimize skipped, backing off after failures")
                return False
            try:
                outcome = self._optimize()
            except (OSError, RuntimeError, ValueError) as exc:
                self._ledger.record_failure(type(exc).__name__)
                logger.warning("sync: optimize failed: %s", exc, exc_info=True)
                return False
            return self._record(outcome)

    def _record(self, outcome: OptimizeOutcome) -> bool:
        """Clear the ledger only for a complete optimize; record every other kind."""
        if outcome.optimized and not outcome.fts_rebuilt:
            self._ledger.record_failure("full-text index rebuild failed")
            return False
        if outcome.optimized:
            self._ledger.clear()
            return True
        if outcome.fragments > 0:
            logger.warning("sync: optimize not run: %s", outcome.reason)
            self._ledger.record_failure(
                "compaction skipped: too many fragments; run 'quarry optimize --force'"
            )
            return False
        return True

    def _optimize(self) -> OptimizeOutcome:
        t0 = time.perf_counter()
        opt = TableOptimizer(self._db)
        opt.create_collection_index()
        logger.info("sync: create_collection_index in %.2fs", time.perf_counter() - t0)
        t0 = time.perf_counter()
        outcome = opt.optimize()
        logger.info("sync: optimize_table in %.2fs", time.perf_counter() - t0)
        return outcome


@final
class ManualOptimize:
    """An operator-triggered optimize: index, compact, then settle the ledger.

    The ledger may hold an index-build failure, so only a run that did both
    steps may clear it; a failure in either propagates and leaves it.
    """

    __slots__ = ("_changes", "_ledger", "_opt")

    _opt: TableOptimizer
    _ledger: FinalizeLedger
    _changes: TableChanges

    def __new__(cls, opt: TableOptimizer, settings: Settings) -> Self:
        """Pair explicit optimization with the database's shared failure ledger."""
        self = super().__new__(cls)
        self._opt = opt
        self._ledger = FinalizeLedgers.for_database(
            settings.lancedb_path, settings.quarry_root
        )
        self._changes = TableChanges.for_path(settings.lancedb_path)
        return self

    def task_results(self, *, force: bool) -> dict[str, object]:
        """Run the optimize and return the fields its task reports."""
        return OptimizeReport(self.run_manually(force=force), force=force).results()

    def run_manually(self, *, force: bool) -> OptimizeOutcome:
        """Create the collection index, compact, and clear the ledger on success."""
        with self._changes.optimizing():
            self._opt.create_collection_index()
            outcome = self._opt.optimize(force=force)
            if outcome.optimized and outcome.fts_rebuilt:
                self._ledger.clear()
            elif outcome.optimized:
                # Compaction renumbered the fragments, so the old index is stale:
                # dirty the record so the next finalize rebuilds it.
                self._ledger.record_failure("full-text index rebuild failed")
                self._changes.note_write()
            return outcome


@final
class OptimizeReport:
    """Turn what the optimizer did into the optimize task's result fields.

    ``optimized`` is whatever the optimizer did: a fragment-count skip is
    ``False`` with a ``reason``, never a false success.  A run that compacted but
    failed to rebuild the full-text index reports ``fts_rebuilt: false`` and says
    so, because the old index is stale until the next finalize rebuilds it.
    """

    __slots__ = ("_force", "_outcome")

    _force: bool
    _outcome: OptimizeOutcome

    def __new__(cls, outcome: OptimizeOutcome, *, force: bool) -> Self:
        """Bind the optimizer's outcome and whether the run was forced."""
        self = super().__new__(cls)
        self._outcome = outcome
        self._force = force
        return self

    def results(self) -> dict[str, object]:
        """Return the task's result fields."""
        outcome = self._outcome
        results: dict[str, object] = {
            "optimized": outcome.optimized,
            "fragments_before": outcome.fragments,
            "force": self._force,
        }
        if outcome.optimized:
            results["fts_rebuilt"] = outcome.fts_rebuilt
        if outcome.optimized and not outcome.fts_rebuilt:
            results["reason"] = _FTS_FAILED
        elif outcome.reason:
            results["reason"] = outcome.reason
        return results
