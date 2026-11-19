"""Table optimization: compaction, FTS rebuild, and collection indexing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Self

from lancedb.index import FTS, Bitmap

from quarry.db.schema import TABLE_NAME
from quarry.types import LanceDB

logger = logging.getLogger(__name__)

FRAGMENT_THRESHOLD: int = 10_000


@dataclass(frozen=True, slots=True)
class OptimizeOutcome:
    """Whether a compaction actually ran, plus the fragment count and a reason.

    ``optimized`` is ``False`` when the table is absent or the fragment count
    exceeded the safety threshold without ``force``; ``reason`` then explains
    the skip.  Callers report this instead of assuming success.
    """

    optimized: bool
    fragments: int
    reason: str = ""


class TableOptimizer:
    """Compact, index, and maintain the LanceDB chunks table."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        """Bind compaction and index maintenance to an open database."""
        self = super().__new__(cls)
        self._db = db
        return self

    def count_fragments(self) -> int:
        """Count data fragments in the chunks table.

        Approximates fragment count by counting entries in the lance data
        directory on disk.  Returns 0 if the table does not exist or the
        data directory cannot be enumerated.
        """
        if TABLE_NAME not in self._db.list_tables().tables:
            return 0
        table = self._db.open_table(TABLE_NAME)
        # LanceDB does not expose fragment count via the Python API. The best
        # proxy is counting subdirectories under the lance ``data/`` dir.
        try:
            data_dir = Path(table.uri) / "data"
            return sum(1 for _ in data_dir.iterdir()) if data_dir.is_dir() else 0
        except (OSError, TypeError, AttributeError):
            # Best-effort: a missing dir or a surprising uri (non-str, absent)
            # degrades to 0 rather than breaking optimize()'s fragment check.
            return 0

    def optimize(self, *, force: bool = False) -> OptimizeOutcome:
        """Compact table data and rebuild the FTS index; report what happened.

        Merges small data fragments for better query performance, then
        rebuilds the Tantivy full-text index so it references the new
        fragment layout.  Without the rebuild, the FTS index retains stale
        row references to compacted-away fragments, causing RuntimeError
        on hybrid retrieval queries (HybridRetriever.retrieve).

        Also prunes old manifest versions older than 1 hour to reclaim
        disk space from the ``_versions/`` directory.

        When the fragment count exceeds ``FRAGMENT_THRESHOLD`` (10,000),
        optimization is skipped to prevent a compaction death spiral -- unless
        *force* is True.  The operator should run ``quarry optimize --force``
        manually for degraded databases.

        Returns an :class:`OptimizeOutcome`: ``optimized`` is ``False`` (with a
        ``reason``) when the table is absent or the compaction was skipped, so a
        caller never reports a skip as success.  The fragment count is taken
        once here, so callers read it from the outcome rather than re-counting.
        """
        if TABLE_NAME not in self._db.list_tables().tables:
            return OptimizeOutcome(optimized=False, fragments=0, reason="no table")

        fragments = self.count_fragments()
        if not force and fragments > FRAGMENT_THRESHOLD:
            logger.warning(
                "LanceDB table has %d fragments (threshold: %d). "
                "Skipping optimization — manual compaction required. "
                "Run: quarry optimize --force",
                fragments,
                FRAGMENT_THRESHOLD,
            )
            reason = (
                f"{fragments} fragments exceed threshold "
                f"({FRAGMENT_THRESHOLD:,}); run with force to override"
            )
            return OptimizeOutcome(optimized=False, fragments=fragments, reason=reason)

        table = self._db.open_table(TABLE_NAME)
        table.optimize(cleanup_older_than=timedelta(hours=1))
        logger.info("Optimized table %s (compacted + pruned versions >1h)", TABLE_NAME)

        # Rebuild the FTS index -- compaction changes fragment IDs, so the old
        # index has stale references.  This builds LanceDB's native (Rust) FTS
        # index on lance's own runtime, bounded by LANCE_CPU_THREADS (DES-032);
        # replace=True forces a full O(n) rebuild (the watch loop rate-limits it).
        try:
            table.create_index("text", config=FTS(), replace=True)
            logger.info("Rebuilt FTS index after optimization")
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "FTS index rebuild after optimize failed; "
                "hybrid search may use vector-only until next sync",
                exc_info=True,
            )
        return OptimizeOutcome(optimized=True, fragments=fragments)

    def create_collection_index(self) -> None:
        """Create a BITMAP scalar index on the collection column.

        Speeds up pre-filtering by collection during vector search.
        Safe to call repeatedly -- uses replace=True.
        """
        if TABLE_NAME not in self._db.list_tables().tables:
            return

        table = self._db.open_table(TABLE_NAME)
        table.create_index("collection", config=Bitmap(), replace=True)
        logger.info("Created BITMAP index on collection column")
