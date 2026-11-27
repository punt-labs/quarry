"""Rebuild a table's full-text index after compaction made the old one stale."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self, final

from lancedb.index import FTS

if TYPE_CHECKING:
    from quarry.types import LanceTable

logger = logging.getLogger(__name__)


@final
class FtsRebuild:
    """Replace the full-text index of one table, reporting whether it worked."""

    __slots__ = ("_table",)

    _table: LanceTable

    def __new__(cls, table: LanceTable) -> Self:
        """Bind the rebuild to an open chunks table."""
        self = super().__new__(cls)
        self._table = table
        return self

    def run(self) -> bool:
        """Rebuild the index; return ``False`` if it failed and left the old one.

        Compaction changes fragment IDs, so the old index has stale references
        until it is rebuilt.  This builds LanceDB's native (Rust) FTS index on
        lance's own runtime, bounded by LANCE_CPU_THREADS (DES-032); replace=True
        forces a full O(n) rebuild, which the finalize gates on a changed table.
        A failure is logged and reported, never raised: the caller must not
        record the table as finalized, so the next finalize rebuilds it.
        """
        try:
            self._table.create_index("text", config=FTS(), replace=True)
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "FTS index rebuild after optimize failed; hybrid search may use "
                "vector-only until the finalize retries after its backoff",
                exc_info=True,
            )
            return False
        logger.info("Rebuilt FTS index after optimization")
        return True
