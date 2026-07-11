"""Post-sync finalization: index rebuild, optimize, capture-shadow push, and GC."""

from __future__ import annotations

import gc
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.db import TableOptimizer

if TYPE_CHECKING:
    from quarry.config import Settings
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

        Fail-open: a push/network/git failure never blocks the sync.  The local
        import bounds this module's efferent coupling.
        """
        from quarry.shadow import CaptureSync  # noqa: PLC0415

        CaptureSync.push_registered(self._settings, fail_open=True)

    def _gc(self) -> None:
        t_gc = time.perf_counter()
        gc.collect(2)
        gc_elapsed = time.perf_counter() - t_gc
        rss_str = "unknown"
        try:
            with Path("/proc/self/status").open() as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_str = line.split(":")[1].strip()
                        break
        except OSError:
            pass
        logger.info("sync: post-sync GC in %.2fs, RSS: %s", gc_elapsed, rss_str)
