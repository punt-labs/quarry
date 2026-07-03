"""Chunk storage: insert, progressive-flush, delete, and count operations."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Self

from quarry._sql import escape_sql
from quarry.db.chunk_table import ChunkTable, DocumentRef
from quarry.db.schema import TABLE_NAME
from quarry.types import LanceDB

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk
    from quarry.types import LanceTable

logger = logging.getLogger(__name__)

# Serializes every ``table.add()`` and ``table.delete()`` so the progressive
# sync consumer's flushes never race a producer thread's overwrite-delete.
# LanceDB's ``add`` is not internally serialized (DES-034 §2), so the single
# consumer plus this lock are what keep writes conflict-free.
_write_lock = threading.Lock()


class ChunkStore:
    """Insert, delete, and count chunks in the LanceDB chunks table."""

    __slots__ = ("_db", "_table")

    _db: LanceDB
    _table: ChunkTable

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        self._table = ChunkTable(db)
        return self

    def _open(self) -> LanceTable | None:
        """Return the chunks table for reads/deletes, or ``None`` if absent."""
        return self._table.open_or_none()

    def insert(
        self,
        chunks: list[Chunk],
        vectors: NDArray[np.float32],
    ) -> int:
        """Insert chunks with their embedding vectors into LanceDB."""
        return self.insert_records(ChunkTable.build_records(chunks, vectors))

    def insert_records(self, records: list[dict[str, object]]) -> int:
        """Append pre-built row dicts to LanceDB — the per-flush write primitive.

        Serialized via ``_write_lock`` so a progressive flush never races a
        producer thread's overwrite-delete (DES-034 §2).
        """
        if not records:
            return 0
        table = self._table.get_or_create(records)
        if table is not None:
            with _write_lock:
                table.add(records)
        logger.info("Inserted %d chunks into %s", len(records), TABLE_NAME)
        return len(records)

    def delete_document(
        self,
        document_name: str,
        collection: str | None = None,
        *,
        count: bool = True,
    ) -> int:
        """Delete all chunks for a document, optionally scoped to a collection."""
        table = self._open()
        if table is None:
            return 0
        predicate = DocumentRef(document_name, collection).predicate()
        if not count:
            with _write_lock:
                table.delete(predicate)
            logger.info("Issued chunk delete for %s (counting disabled)", document_name)
            return 0
        deleted = table.count_rows(predicate)
        with _write_lock:
            table.delete(predicate)
        logger.info("Deleted %d chunks for %s", deleted, document_name)
        return deleted

    def delete_document_tail(self, ref: DocumentRef) -> int:
        """Delete *ref*'s chunks with ``chunk_index >= ref.min_chunk_index``.

        The within-file resume reconciliation primitive (DES-034 §5.3): before
        re-embedding from a watermark ``w``, this clears any possibly-partial
        post-watermark write ``[w, K)`` while preserving the confirmed prefix
        ``[0, w)``, making resume idempotent under repeated crashes. Returns the
        number of rows removed.

        Raises ``ValueError`` if *ref* has no positive watermark — a tail delete
        with ``min_chunk_index <= 0`` would drop the whole document; use
        ``delete_document`` for that.
        """
        if ref.min_chunk_index <= 0:
            msg = f"delete_document_tail needs a positive watermark, got {ref!r}"
            raise ValueError(msg)
        table = self._open()
        if table is None:
            return 0
        predicate = ref.predicate()
        deleted = table.count_rows(predicate)
        with _write_lock:
            table.delete(predicate)
        logger.info(
            "Delete-tail removed %d chunks for %s (chunk_index >= %d)",
            deleted,
            ref.document_name,
            ref.min_chunk_index,
        )
        return deleted

    def delete_collection(self, collection: str) -> int:
        """Delete all chunks in a collection."""
        table = self._open()
        if table is None:
            return 0
        before = table.count_rows()
        with _write_lock:
            table.delete(f"collection = '{escape_sql(collection)}'")
        after = table.count_rows()
        deleted = before - after
        logger.info("Deleted %d chunks for collection %s", deleted, collection)
        return deleted

    def count(self, collection_filter: str | None = None) -> int:
        """Return the total number of chunks, optionally filtered by collection."""
        table = self._open()
        if table is None:
            return 0
        if collection_filter:
            return table.count_rows(f"collection = '{escape_sql(collection_filter)}'")
        return table.count_rows()
