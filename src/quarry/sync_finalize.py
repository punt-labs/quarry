"""Post-sync finalization: index rebuild, optimize, capture-shadow push, and GC."""

from __future__ import annotations

import gc
import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.db import TableOptimizer

if TYPE_CHECKING:
    from quarry.config import Settings
    from quarry.shadow import ShadowSyncResult
    from quarry.types import LanceDB

logger = logging.getLogger(__name__)


@final
class SyncFinalizer:
    """Rebuild the index, optimize, push enabled shadows, then GC after a sync."""

    __slots__ = ("_db", "_settings")

    _db: LanceDB
    _settings: Settings

    def __new__(cls, db: LanceDB, settings: Settings) -> Self:
        self = super().__new__(cls)
        self._db = db
        self._settings = settings
        return self

    def run(self) -> None:
        """Optimize the table, push enabled capture shadows, then GC."""
        self._optimize()
        self._push_shadows()
        self._gc()

    def _optimize(self) -> None:
        t0 = time.perf_counter()
        opt = TableOptimizer(self._db)
        opt.create_collection_index()
        logger.info("sync: create_collection_index in %.2fs", time.perf_counter() - t0)
        t0 = time.perf_counter()
        opt.optimize()
        logger.info("sync: optimize_table in %.2fs", time.perf_counter() - t0)

    def _push_shadows(self) -> None:
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
        """Log each enabled shadow whose captures did not reach the remote."""
        for collection, result in results.items():
            if not result.pushed:
                reason = result.aborted_reason or "push failed"
                logger.warning(
                    "sync: %s captures committed but not pushed to shadow (%s)",
                    collection,
                    reason,
                )

    def _gc(self) -> None:
        t_gc = time.perf_counter()
        gc.collect(2)
        elapsed = time.perf_counter() - t_gc
        logger.info("sync: post-sync GC in %.2fs, RSS: %s", elapsed, self._read_rss())

    @staticmethod
    def _read_rss() -> str:
        """Return the process RSS from ``/proc`` (``"unknown"`` off Linux)."""
        try:
            with Path("/proc/self/status").open() as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return line.split(":")[1].strip()
        except OSError:
            pass
        return "unknown"
