"""Lifecycle of the LanceDB chunks table: open, migrate, and create-on-demand."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Self

from quarry.db.row_budget import RowBudget
from quarry.db.schema import TABLE_NAME, SchemaManager
from quarry.types import LanceDB, LanceTable

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk

logger = logging.getLogger(__name__)

# Guards ``create_table`` against concurrent creation (double-checked below).
_table_lock = threading.Lock()


class ChunkTable:
    """Open, migrate, and create the LanceDB chunks table on demand."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    @staticmethod
    def build_records(
        chunks: list[Chunk],
        vectors: NDArray[np.float32],
    ) -> list[dict[str, object]]:
        """Build LanceDB row dicts from chunks and their embedding vectors.

        The one place every writer builds rows, so it is where the row budget is
        enforced: a page or chunk over budget raises, never truncates.
        """
        budget = RowBudget()
        records: list[dict[str, object]] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            budget.require_page(chunk.page_raw_text)
            budget.require_chunk(chunk.text)
            record: dict[str, object] = asdict(chunk)
            budget.require_labels(record)
            record["vector"] = vector.tolist()
            records.append(record)
        return records

    def open_or_none(self) -> LanceTable | None:
        """Return the chunks table, or ``None`` when it does not exist.

        Tolerates a stale ``list_tables`` cache that lists the table but whose
        ``open_table`` then raises a "not found" ``ValueError``.
        """
        if not self._is_listed():
            return None
        try:
            return self._db.open_table(TABLE_NAME)
        except ValueError as exc:
            if "not found" not in str(exc).lower():
                raise
            return None

    def open_migrated(self) -> LanceTable | None:
        """Open the table, running schema migration and FTS index creation."""
        table = self.open_or_none()
        if table is None:
            return None
        mgr = SchemaManager(self._db)
        mgr.migrate(table)
        mgr.ensure_fts_index(table)
        return table

    def get_or_create(
        self,
        records: Sequence[dict[str, object]],
    ) -> LanceTable | None:
        """Return the table for appending, creating it with *records* if needed.

        Returns ``None`` when the table was just created (``create_table``
        inserts *records* as part of creation). Uses double-checked locking so
        concurrent sync workers cannot race table creation.
        """
        table = self._open_listed()
        if table is not None:
            return table
        with _table_lock:
            table = self._open_listed()
            if table is not None:
                return table
            try:
                table = self._db.create_table(
                    TABLE_NAME, data=list(records), schema=SchemaManager.schema()
                )
            except ValueError as exc:
                if "already exists" not in str(exc).lower():
                    raise
                logger.debug("create_table raced, opening existing")
                table = self.open_migrated()
                if table is None:
                    raise RuntimeError(
                        f"Table {TABLE_NAME} reported as existing but cannot be opened"
                    ) from exc
                return table
            SchemaManager(self._db).ensure_fts_index(table)
            return None

    def _is_listed(self) -> bool:
        return TABLE_NAME in self._db.list_tables().tables

    def _open_listed(self) -> LanceTable | None:
        """Open the table if it is listed; ``None`` when absent or unopenable."""
        if not self._is_listed():
            return None
        table = self.open_migrated()
        if table is None:
            logger.debug("open_table failed after list_tables, creating")
        return table
