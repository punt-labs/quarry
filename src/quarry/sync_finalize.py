"""Post-sync finalization: index rebuild, optimize, capture-shadow push, and GC."""

from __future__ import annotations

import gc
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.db.orphan_pages import OrphanPageSweep
from quarry.finalize_steps import OptimizeStep, ShadowPushStep

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
        """Bind index maintenance and shadow pushes to a database and settings."""
        self = super().__new__(cls)
        self._db = db
        self._settings = settings
        return self

    def run(self) -> None:
        """Optimize, sweep orphan pages, push shadows, then GC, each isolated."""
        self._optimize()
        self._sweep_pages()
        self._push_shadows()
        self._gc()

    def _optimize(self) -> None:
        OptimizeStep(self._db, self._settings).run()

    def _sweep_pages(self) -> None:
        try:
            OrphanPageSweep(self._db).run()
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning("sync: orphan page sweep failed: %s", exc, exc_info=True)

    def _push_shadows(self) -> None:
        ShadowPushStep(self._settings).run()

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
