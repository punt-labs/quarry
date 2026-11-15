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
    from quarry.shadow import ShadowSyncResult
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@final
class OptimizeStep:
    """Optimize one database unless backed off; a failure is recorded, not raised.

    Cleanup shares ``table.optimize()`` with compaction, so a failing optimize
    leaks orphaned files on every attempt.  The failure must not stop the rest of
    the finalize, and the ledger spaces the retries out.
    """

    __slots__ = ("_db", "_ledger")

    _db: LanceDB
    _ledger: FinalizeLedger

    def __new__(cls, db: LanceDB, database_key: str) -> Self:
        self = super().__new__(cls)
        self._db = db
        self._ledger = FinalizeLedgers.for_database(database_key)
        return self

    def run(self) -> None:
        """Rebuild indexes and compact, or skip while backing off."""
        if self._ledger.is_backed_off():
            logger.warning("sync: optimize skipped, backing off after failures")
            return
        try:
            self._optimize()
        except (OSError, RuntimeError, ValueError) as exc:
            self._ledger.record_failure(f"{type(exc).__name__}: {exc}")
            logger.warning("sync: optimize failed: %s", exc, exc_info=True)
            return
        self._ledger.record_success()

    def _optimize(self) -> None:
        t0 = time.perf_counter()
        opt = TableOptimizer(self._db)
        opt.create_collection_index()
        logger.info("sync: create_collection_index in %.2fs", time.perf_counter() - t0)
        t0 = time.perf_counter()
        opt.optimize()
        logger.info("sync: optimize_table in %.2fs", time.perf_counter() - t0)


@final
class ShadowPushStep:
    """Push every enabled project's captures to its private shadow."""

    __slots__ = ("_settings",)

    _settings: Settings

    def __new__(cls, settings: Settings) -> Self:
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
