"""Lifecycle of the LanceDB chunks table: open, migrate, and create-on-demand."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Self

from quarry._sql import escape_sql
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


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """A scoped selector for a document's chunks.

    Owns the SQL predicate that selects the document's rows. ``min_chunk_index``
    of 0 selects the whole document; a positive watermark restricts to the resume
    tail ``chunk_index >= min_chunk_index`` (DES-034 §5.3), so a single value
    object drives both full-document and tail deletes.
    """

    document_name: str
    collection: str | None = None
    min_chunk_index: int = 0

    def __post_init__(self) -> None:
        if not self.document_name:
            msg = "DocumentRef.document_name must be non-empty"
            raise ValueError(msg)
        if self.min_chunk_index < 0:
            msg = f"min_chunk_index must be >= 0, got {self.min_chunk_index}"
            raise ValueError(msg)

    def predicate(self) -> str:
        """Return the document/collection/watermark selection clause."""
        clause = f"document_name = '{escape_sql(self.document_name)}'"
        if self.collection:
            clause += f" AND collection = '{escape_sql(self.collection)}'"
        if self.min_chunk_index > 0:
            clause += f" AND chunk_index >= {self.min_chunk_index}"
        return clause


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
        """Build LanceDB row dicts from chunks and their embedding vectors."""
        records: list[dict[str, object]] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            record: dict[str, object] = asdict(chunk)
            record["vector"] = vector.tolist()
            records.append(record)
        return records

    def open_or_none(self) -> LanceTable | None:
        """Return the chunks table, or ``None`` when it does not exist.

        Tolerates a stale ``list_tables`` cache that lists the table but whose
        ``open_table`` then raises a "not found" ``ValueError``.
        """
        if TABLE_NAME not in self._db.list_tables().tables:
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
        mgr = SchemaManager(self._db)
        if TABLE_NAME in self._db.list_tables().tables:
            table = self.open_migrated()
            if table is not None:
                return table
            logger.debug("open_table failed after list_tables, creating")
        with _table_lock:
            if TABLE_NAME in self._db.list_tables().tables:
                table = self.open_migrated()
                if table is not None:
                    return table
                logger.debug("Table listed but open_table failed under lock, creating")
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
            mgr.ensure_fts_index(table)
            return None
