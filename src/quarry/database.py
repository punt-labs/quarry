from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pyarrow as pa

from quarry.models import Chunk
from quarry.types import LanceDB

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

TABLE_NAME = "ocr_chunks"


def _escape_sql(value: str) -> str:
    """Escape single quotes for LanceDB SQL predicates."""
    return value.replace("'", "''")


def _schema(embedding_dimension: int = 768) -> pa.Schema:
    return pa.schema(
        [
            pa.field("text", pa.utf8()),
            pa.field("vector", pa.list_(pa.float32(), embedding_dimension)),
            pa.field("document_name", pa.utf8()),
            pa.field("document_path", pa.utf8()),
            pa.field("page_number", pa.int32()),
            pa.field("total_pages", pa.int32()),
            pa.field("chunk_index", pa.int32()),
            pa.field("page_raw_text", pa.utf8()),
            pa.field("ingestion_timestamp", pa.timestamp("us", tz="UTC")),
        ]
    )


def get_db(db_path: Path) -> LanceDB:
    import lancedb  # noqa: PLC0415

    db_path.mkdir(parents=True, exist_ok=True)
    return cast("LanceDB", lancedb.connect(str(db_path)))  # type: ignore[attr-defined]


def insert_chunks(
    db: LanceDB,
    chunks: list[Chunk],
    vectors: NDArray[np.float32],
) -> int:
    """Insert chunks with their embedding vectors into LanceDB.

    Args:
        db: LanceDB connection.
        chunks: Document chunks with metadata.
        vectors: Embedding vectors, shape (len(chunks), 768).

    Returns:
        Number of rows inserted.
    """
    records: list[dict[str, object]] = []
    for chunk, vector in zip(chunks, vectors, strict=True):
        record: dict[str, object] = asdict(chunk)
        record["vector"] = vector.tolist()
        records.append(record)

    if TABLE_NAME in db.list_tables().tables:
        table = db.open_table(TABLE_NAME)
        table.add(records)
    else:
        db.create_table(TABLE_NAME, data=records, schema=_schema())

    logger.info("Inserted %d chunks into %s", len(records), TABLE_NAME)
    return len(records)


def search(
    db: LanceDB,
    query_vector: NDArray[np.float32],
    limit: int = 10,
    document_filter: str | None = None,
) -> list[dict[str, object]]:
    """Search for similar chunks using vector similarity.

    Args:
        db: LanceDB connection.
        query_vector: Query embedding vector.
        limit: Maximum results to return.
        document_filter: Optional document name filter (exact match).

    Returns:
        List of result dicts with text, metadata, and _distance.
    """
    logger.debug("Search: limit=%d, document_filter=%s", limit, document_filter)

    if TABLE_NAME not in db.list_tables().tables:
        logger.debug("Search: table %s not found, returning empty", TABLE_NAME)
        return []

    table = db.open_table(TABLE_NAME)
    query = table.search(query_vector.tolist()).limit(limit)

    if document_filter:
        query = query.where(f"document_name = '{_escape_sql(document_filter)}'")

    results = query.to_list()
    logger.debug("Search: %d results returned", len(results))
    return results


def get_page_text(
    db: LanceDB,
    document_name: str,
    page_number: int,
) -> str | None:
    """Retrieve the full raw text for a specific page.

    Args:
        db: LanceDB connection.
        document_name: Document filename.
        page_number: 1-indexed page number.

    Returns:
        Raw page text, or None if not found.
    """
    logger.debug("get_page_text: document=%s, page=%d", document_name, page_number)

    if TABLE_NAME not in db.list_tables().tables:
        logger.debug("get_page_text: table %s not found", TABLE_NAME)
        return None

    table = db.open_table(TABLE_NAME)
    results = (
        table.search()
        .where(
            f"document_name = '{_escape_sql(document_name)}'"
            f" AND page_number = {page_number}"
        )
        .limit(1)
        .select(["page_raw_text"])
        .to_list()
    )

    if not results:
        logger.debug("get_page_text: no results found")
        return None
    return str(results[0]["page_raw_text"])


def list_documents(db: LanceDB) -> list[dict[str, object]]:
    """List all indexed documents with metadata.

    Returns:
        List of dicts with document_name, document_path, total_pages,
        chunk_count, and ingestion_timestamp.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return []

    table = db.open_table(TABLE_NAME)
    rows = (
        table.search()
        .select(
            [
                "document_name",
                "document_path",
                "total_pages",
                "page_number",
                "ingestion_timestamp",
            ]
        )
        .to_list()
    )

    if not rows:
        return []

    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        name = str(row["document_name"])
        if name not in grouped:
            grouped[name] = []
        grouped[name].append(row)

    docs: list[dict[str, object]] = []
    for name, chunks in grouped.items():
        pages = {int(str(c["page_number"])) for c in chunks}
        docs.append(
            {
                "document_name": name,
                "document_path": str(chunks[0]["document_path"]),
                "total_pages": int(str(chunks[0]["total_pages"])),
                "chunk_count": len(chunks),
                "indexed_pages": len(pages),
                "ingestion_timestamp": str(
                    max(str(c["ingestion_timestamp"]) for c in chunks)
                ),
            }
        )

    return docs


def count_chunks(db: LanceDB) -> int:
    """Return the total number of chunks across all documents."""
    if TABLE_NAME not in db.list_tables().tables:
        return 0
    return db.open_table(TABLE_NAME).count_rows()


def delete_document(db: LanceDB, document_name: str) -> int:
    """Delete all chunks for a document.

    Args:
        db: LanceDB connection.
        document_name: Document filename to delete.

    Returns:
        Number of rows deleted (0 if document not found).
    """
    if TABLE_NAME not in db.list_tables().tables:
        return 0

    table = db.open_table(TABLE_NAME)
    before = table.count_rows()
    table.delete(f"document_name = '{_escape_sql(document_name)}'")
    after = table.count_rows()
    deleted = before - after
    logger.info("Deleted %d chunks for %s", deleted, document_name)
    return deleted
