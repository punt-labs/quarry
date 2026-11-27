"""The garbage collection that follows a table rewrite, and the RSS it reports."""

from __future__ import annotations

import gc
import logging
import time
from pathlib import Path
from typing import final

logger = logging.getLogger(__name__)


@final
class PostSyncGc:
    """Collect every generation so a superseded index connection is reclaimed."""

    __slots__ = ()

    def run(self) -> None:
        """Run a full collection and log how long it took and the resident size."""
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
