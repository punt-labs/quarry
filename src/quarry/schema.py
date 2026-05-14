"""Schema management for the LanceDB chunks table."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Self

import pyarrow as pa

if TYPE_CHECKING:
    from quarry.types import LanceDB, LanceTable

logger = logging.getLogger(__name__)

TABLE_NAME = "chunks"

# Columns added by schema migration, with their SQL default expressions.
_MIGRATION_COLUMNS: dict[str, str] = {
    "agent_handle": "''",
    "memory_type": "''",
    "summary": "''",
}


class SchemaManager:
    """Manage the LanceDB chunks table schema: creation, migration, FTS index."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    @classmethod
    def schema(cls, embedding_dimension: int = 768) -> pa.Schema:
        """Return the canonical PyArrow schema for the chunks table."""
        return pa.schema(
            [
                pa.field("text", pa.utf8()),
                pa.field("vector", pa.list_(pa.float32(), embedding_dimension)),
                pa.field("document_name", pa.utf8()),
                pa.field("document_path", pa.utf8()),
                pa.field("collection", pa.utf8()),
                pa.field("page_number", pa.int32()),
                pa.field("total_pages", pa.int32()),
                pa.field("chunk_index", pa.int32()),
                pa.field("page_raw_text", pa.utf8()),
                pa.field("page_type", pa.utf8()),
                pa.field("source_format", pa.utf8()),
                pa.field("ingestion_timestamp", pa.timestamp("us", tz="UTC")),
                pa.field("agent_handle", pa.utf8()),
                pa.field("memory_type", pa.utf8()),
                pa.field("summary", pa.utf8()),
            ]
        )

    def migrate(self, table: LanceTable) -> None:
        """Add missing columns to an existing table.

        Idempotent -- checks the table schema before adding each column.
        Called on every table open so existing databases gain new columns
        transparently.  Logs a warning on failure so the caller can proceed
        with the existing schema.
        """
        existing = {field.name for field in table.schema}
        missing = {
            col: expr for col, expr in _MIGRATION_COLUMNS.items() if col not in existing
        }
        if missing:
            try:
                table.add_columns(missing)
            except (OSError, RuntimeError, ValueError):
                logger.warning(
                    "Schema migration failed for columns %s",
                    sorted(missing),
                    exc_info=True,
                )
                return
            logger.info("Migrated schema: added columns %s", sorted(missing))

    def ensure_fts_index(self, table: LanceTable) -> None:
        """Create a Tantivy full-text search index on the text column if missing.

        Uses replace=False and catches the "already exists" error so this is
        safe to call repeatedly without rebuilding the entire index each time.
        Logs a warning on unexpected failures so callers can fall back to
        vector-only search.
        """
        try:
            table.create_fts_index("text", replace=False)
            logger.info("Created FTS index on text column")
        except (OSError, RuntimeError, ValueError) as exc:
            # LanceDB raises when the index already exists.  Any message
            # containing "already" is the expected idempotent case.
            if "already" in str(exc).lower():
                logger.debug("FTS index already exists, skipping creation")
            else:
                logger.warning(
                    "FTS index creation failed; hybrid search will use vector-only",
                    exc_info=True,
                )

    def ensure(self) -> None:
        """Run schema migration and FTS index creation on an existing table.

        Safe to call at application startup. No-op if the table does not exist.
        """
        if TABLE_NAME not in self._db.list_tables().tables:
            return
        table = self._db.open_table(TABLE_NAME)
        self.migrate(table)
        self.ensure_fts_index(table)
