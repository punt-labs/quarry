"""The isolated finalize steps: optimize with backoff, and the fail-open shadow push."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Self, final

from quarry.db import TableOptimizer
from quarry.finalize_ledger import FinalizeLedger, FinalizeLedgers

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.db.optimizer import OptimizeOutcome
    from quarry.shadow import ShadowSyncResult
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@final
class OptimizeStep:
    """Optimize one database unless backed off; a failure is recorded, not raised.

    Cleanup shares ``table.optimize()`` with compaction, so a failing optimize
    leaks orphaned files on every attempt.  The failure must not stop the rest of
    the finalize, and the ledger spaces the retries out.  The ledger carries only
    the exception type: its snapshot is served unauthenticated on ``/health``,
    while the full error goes to the log.
    """

    __slots__ = ("_db", "_ledger")

    _db: LanceDB
    _ledger: FinalizeLedger

    def __new__(cls, db: LanceDB, settings: Settings) -> Self:
        """Bind optimization to a database and its shared failure-backoff ledger."""
        self = super().__new__(cls)
        self._db = db
        self._ledger = FinalizeLedgers.for_database(
            settings.lancedb_path, settings.quarry_root
        )
        return self

    def run(self) -> None:
        """Rebuild indexes and compact, or skip while backing off."""
        if self._ledger.is_backed_off():
            logger.warning("sync: optimize skipped, backing off after failures")
            return
        try:
            outcome = self._optimize()
        except (OSError, RuntimeError, ValueError) as exc:
            self._ledger.record_failure(type(exc).__name__)
            logger.warning("sync: optimize failed: %s", exc, exc_info=True)
            return
        self._record(outcome)

    def _record(self, outcome: OptimizeOutcome) -> None:
        """Clear the ledger only for a compaction that ran; flag a forced skip."""
        if outcome.optimized:
            self._ledger.clear()
        elif outcome.fragments > 0:
            logger.warning("sync: optimize not run: %s", outcome.reason)
            self._ledger.record_failure(
                "compaction skipped: too many fragments; run 'quarry optimize --force'"
            )

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
class ShadowPushStep:
    """Push every enabled project's captures to its private shadow."""

    __slots__ = ("_settings",)

    _settings: Settings

    def __new__(cls, settings: Settings) -> Self:
        """Retain settings for discovering and pushing enabled capture shadows."""
        self = super().__new__(cls)
        self._settings = settings
        return self

    def run(self) -> None:
        """Push each enabled project's redacted captures to its private shadow.

        Fail-open, and the WHOLE step is wrapped: a registry-open or import
        failure must not raise past here and block the sync tail (index rebuild
        already ran; GC must still run).  A committed-but-not-pushed result is
        logged rather than swallowed silently (fail-open but LOGGED).
        """
        try:
            from quarry.shadow import CaptureSync  # noqa: PLC0415

            results = CaptureSync.push_registered(self._settings, fail_open=True)
        except (ImportError, OSError, sqlite3.Error) as exc:
            logger.warning("sync: capture-shadow push skipped (fail-open): %s", exc)
            return
        self._warn_unpushed(results)

    @staticmethod
    def _warn_unpushed(results: dict[str, ShadowSyncResult]) -> None:
        """Log each enabled shadow whose captures did not reach the remote.

        The message reflects the real state: a run that aborted, a run that
        committed but could not push, or a run that neither committed nor
        aborted (nothing to commit or a deferred offline push).
        """
        for collection, result in results.items():
            if not result.pushed:
                logger.warning(
                    "sync: %s captures %s", collection, result.unpushed_detail()
                )


@final
class ManualOptimize:
    """An operator-triggered optimize: index, compact, then settle the ledger.

    The ledger may hold an index-build failure, so only a run that did both
    steps may clear it; a failure in either propagates and leaves it.
    """

    __slots__ = ("_ledger", "_opt")

    _opt: TableOptimizer
    _ledger: FinalizeLedger

    def __new__(cls, opt: TableOptimizer, settings: Settings) -> Self:
        """Pair explicit optimization with the database's shared failure ledger."""
        self = super().__new__(cls)
        self._opt = opt
        self._ledger = FinalizeLedgers.for_database(
            settings.lancedb_path, settings.quarry_root
        )
        return self

    def run_manually(self, *, force: bool) -> OptimizeOutcome:
        """Create the collection index, compact, and clear the ledger on success."""
        self._opt.create_collection_index()
        outcome = self._opt.optimize(force=force)
        if outcome.optimized:
            self._ledger.clear()
        return outcome
