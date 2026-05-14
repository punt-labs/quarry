"""Chunk storage: insert, batch-insert, delete, and count operations."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Self

from quarry.db.schema import TABLE_NAME, SchemaManager
from quarry.types import LanceDB, LanceTable

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import Chunk

logger = logging.getLogger(__name__)

_table_lock = threading.Lock()


class ChunkStore:
    """Insert, delete, and count chunks in the LanceDB chunks table."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    def _try_open_table(self) -> LanceTable | None:
        """Open, migrate, and index the chunks table.

        Returns the table if it can be opened, or ``None`` when ``open_table``
        raises a "not found" ``ValueError`` (stale ``list_tables`` cache).
        All other ``ValueError`` exceptions are re-raised.
        """
        mgr = SchemaManager(self._db)
        try:
            table = self._db.open_table(TABLE_NAME)
        except ValueError as exc:
            if "not found" not in str(exc).lower():
                raise
            return None
        mgr.migrate(table)
        mgr.ensure_fts_index(table)
        return table

    def _get_or_create_table(
        self,
        records: list[dict[str, object]],
    ) -> LanceTable | None:
        """Return the chunks table, creating it with *records* if needed.

        Returns the table for appending when it already exists, or ``None``
        when the table was just created (``create_table`` inserts *records*
        as part of creation).

        Uses double-checked locking: check outside lock for the common case
        (table exists); only acquire lock when table missing. Prevents races
        when multiple sync workers try to create the table simultaneously.

        Runs schema migration and FTS index creation on every open.
        """
        mgr = SchemaManager(self._db)
        if TABLE_NAME in self._db.list_tables().tables:
            table = self._try_open_table()
            if table is not None:
                return table
            logger.debug("open_table failed after list_tables, creating")
        with _table_lock:
            if TABLE_NAME in self._db.list_tables().tables:
                table = self._try_open_table()
                if table is not None:
                    return table
                logger.debug("Table listed but open_table failed under lock, creating")
            try:
                table = self._db.create_table(
                    TABLE_NAME, data=records, schema=SchemaManager.schema()
                )
            except ValueError as exc:
                if "already exists" not in str(exc).lower():
                    raise
                logger.debug("create_table raced, opening existing")
                table = self._try_open_table()
                if table is None:
                    raise RuntimeError(
                        f"Table {TABLE_NAME} reported as existing but cannot be opened"
                    ) from exc
                return table
            mgr.ensure_fts_index(table)
            return None

    def insert(
        self,
        chunks: list[Chunk],
        vectors: NDArray[np.float32],
    ) -> int:
        """Insert chunks with their embedding vectors into LanceDB."""
        records: list[dict[str, object]] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            record: dict[str, object] = asdict(chunk)
            record["vector"] = vector.tolist()
            records.append(record)

        table = self._get_or_create_table(records)
        if table is not None:
            table.add(records)

        logger.info("Inserted %d chunks into %s", len(records), TABLE_NAME)
        return len(records)

    def batch_insert(
        self,
        batch: list[tuple[list[Chunk], NDArray[np.float32]]],
    ) -> int:
        """Insert multiple documents' chunks in a single LanceDB write."""
        if not batch:
            return 0

        records: list[dict[str, object]] = []
        for chunks, vectors in batch:
            for chunk, vector in zip(chunks, vectors, strict=True):
                record: dict[str, object] = asdict(chunk)
                record["vector"] = vector.tolist()
                records.append(record)

        if not records:
            return 0

        table = self._get_or_create_table(records)
        if table is not None:
            table.add(records)

        logger.info("Batch-inserted %d chunks into %s", len(records), TABLE_NAME)
        return len(records)

    def delete_document(
        self,
        document_name: str,
        collection: str | None = None,
        *,
        count: bool = True,
    ) -> int:
        """Delete all chunks for a document, optionally scoped to a collection."""
        from quarry._sql import escape_sql  # noqa: PLC0415

        if TABLE_NAME not in self._db.list_tables().tables:
            return 0

        try:
            table = self._db.open_table(TABLE_NAME)
        except ValueError as exc:
            if "not found" not in str(exc).lower():
                raise
            return 0
        predicate = f"document_name = '{escape_sql(document_name)}'"
        if collection:
            predicate += f" AND collection = '{escape_sql(collection)}'"

        if not count:
            table.delete(predicate)
            logger.info("Issued chunk delete for %s (counting disabled)", document_name)
            return 0

        deleted = table.count_rows(predicate)
        table.delete(predicate)
        logger.info("Deleted %d chunks for %s", deleted, document_name)
        return deleted

    def delete_collection(self, collection: str) -> int:
        """Delete all chunks in a collection."""
        from quarry._sql import escape_sql  # noqa: PLC0415

        if TABLE_NAME not in self._db.list_tables().tables:
            return 0

        table = self._db.open_table(TABLE_NAME)
        before = table.count_rows()
        table.delete(f"collection = '{escape_sql(collection)}'")
        after = table.count_rows()
        deleted = before - after
        logger.info("Deleted %d chunks for collection %s", deleted, collection)
        return deleted

    def count(self, collection_filter: str | None = None) -> int:
        """Return the total number of chunks, optionally filtered by collection."""
        from quarry._sql import escape_sql  # noqa: PLC0415

        if TABLE_NAME not in self._db.list_tables().tables:
            return 0
        table = self._db.open_table(TABLE_NAME)
        if collection_filter:
            return table.count_rows(f"collection = '{escape_sql(collection_filter)}'")
        return table.count_rows()
