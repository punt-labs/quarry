from __future__ import annotations

import logging
import threading
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
_table_lock = threading.Lock()


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
            pa.field("collection", pa.utf8()),
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
        with _table_lock:
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
    collection_filter: str | None = None,
) -> list[dict[str, object]]:
    """Search for similar chunks using vector similarity.

    Args:
        db: LanceDB connection.
        query_vector: Query embedding vector.
        limit: Maximum results to return.
        document_filter: Optional document name filter (exact match).
        collection_filter: Optional collection name filter (pre-filter).

    Returns:
        List of result dicts with text, metadata, and _distance.
    """
    logger.debug(
        "Search: limit=%d, document_filter=%s, collection_filter=%s",
        limit,
        document_filter,
        collection_filter,
    )

    if TABLE_NAME not in db.list_tables().tables:
        logger.debug("Search: table %s not found, returning empty", TABLE_NAME)
        return []

    table = db.open_table(TABLE_NAME)
    query = table.search(query_vector.tolist()).limit(limit)

    predicates: list[str] = []
    if document_filter:
        predicates.append(f"document_name = '{_escape_sql(document_filter)}'")
    if collection_filter:
        predicates.append(f"collection = '{_escape_sql(collection_filter)}'")
    if predicates:
        query = query.where(" AND ".join(predicates))

    results = query.to_list()
    logger.debug("Search: %d results returned", len(results))
    return results


def get_page_text(
    db: LanceDB,
    document_name: str,
    page_number: int,
    collection: str | None = None,
) -> str | None:
    """Retrieve the full raw text for a specific page.

    Args:
        db: LanceDB connection.
        document_name: Document filename.
        page_number: 1-indexed page number.
        collection: Optional collection scope.

    Returns:
        Raw page text, or None if not found.
    """
    logger.debug("get_page_text: document=%s, page=%d", document_name, page_number)

    if TABLE_NAME not in db.list_tables().tables:
        logger.debug("get_page_text: table %s not found", TABLE_NAME)
        return None

    predicate = (
        f"document_name = '{_escape_sql(document_name)}'"
        f" AND page_number = {page_number}"
    )
    if collection:
        predicate += f" AND collection = '{_escape_sql(collection)}'"

    table = db.open_table(TABLE_NAME)
    results = (
        table.search().where(predicate).limit(1).select(["page_raw_text"]).to_list()
    )

    if not results:
        logger.debug("get_page_text: no results found")
        return None
    return str(results[0]["page_raw_text"])


def list_documents(
    db: LanceDB,
    collection_filter: str | None = None,
) -> list[dict[str, object]]:
    """List all indexed documents with metadata.

    Args:
        db: LanceDB connection.
        collection_filter: Optional collection name filter.

    Returns:
        List of dicts with document_name, document_path, collection,
        total_pages, chunk_count, and ingestion_timestamp.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return []

    table = db.open_table(TABLE_NAME)
    query = table.search().select(
        [
            "document_name",
            "document_path",
            "collection",
            "total_pages",
            "page_number",
            "ingestion_timestamp",
        ]
    )
    if collection_filter:
        query = query.where(f"collection = '{_escape_sql(collection_filter)}'")
    rows = query.to_list()

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
                "collection": str(chunks[0]["collection"]),
                "total_pages": int(str(chunks[0]["total_pages"])),
                "chunk_count": len(chunks),
                "indexed_pages": len(pages),
                "ingestion_timestamp": str(
                    max(str(c["ingestion_timestamp"]) for c in chunks)
                ),
            }
        )

    return docs


def count_chunks(
    db: LanceDB,
    collection_filter: str | None = None,
) -> int:
    """Return the total number of chunks, optionally filtered by collection."""
    if TABLE_NAME not in db.list_tables().tables:
        return 0
    table = db.open_table(TABLE_NAME)
    if collection_filter:
        return table.count_rows(f"collection = '{_escape_sql(collection_filter)}'")
    return table.count_rows()


def delete_document(
    db: LanceDB,
    document_name: str,
    collection: str | None = None,
) -> int:
    """Delete all chunks for a document, optionally scoped to a collection.

    Args:
        db: LanceDB connection.
        document_name: Document filename to delete.
        collection: If provided, only delete within this collection.

    Returns:
        Number of rows deleted (0 if document not found).
    """
    if TABLE_NAME not in db.list_tables().tables:
        return 0

    table = db.open_table(TABLE_NAME)
    before = table.count_rows()
    predicate = f"document_name = '{_escape_sql(document_name)}'"
    if collection:
        predicate += f" AND collection = '{_escape_sql(collection)}'"
    table.delete(predicate)
    after = table.count_rows()
    deleted = before - after
    logger.info("Deleted %d chunks for %s", deleted, document_name)
    return deleted


def list_collections(db: LanceDB) -> list[dict[str, object]]:
    """List all collections with document and chunk counts.

    Returns:
        List of dicts with collection, document_count, chunk_count.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return []

    table = db.open_table(TABLE_NAME)
    rows = table.search().select(["collection", "document_name"]).to_list()
    if not rows:
        return []

    grouped: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for row in rows:
        col = str(row["collection"])
        if col not in grouped:
            grouped[col] = set()
            counts[col] = 0
        grouped[col].add(str(row["document_name"]))
        counts[col] += 1

    return [
        {
            "collection": col,
            "document_count": len(docs),
            "chunk_count": counts[col],
        }
        for col, docs in sorted(grouped.items())
    ]


def delete_collection(db: LanceDB, collection: str) -> int:
    """Delete all chunks in a collection.

    Args:
        db: LanceDB connection.
        collection: Collection name to delete.

    Returns:
        Number of rows deleted.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return 0

    table = db.open_table(TABLE_NAME)
    before = table.count_rows()
    table.delete(f"collection = '{_escape_sql(collection)}'")
    after = table.count_rows()
    deleted = before - after
    logger.info("Deleted %d chunks for collection %s", deleted, collection)
    return deleted


def create_collection_index(db: LanceDB) -> None:
    """Create a BITMAP scalar index on the collection column.

    Speeds up pre-filtering by collection during vector search.
    Safe to call repeatedly — uses replace=True.

    Args:
        db: LanceDB connection.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return

    table = db.open_table(TABLE_NAME)
    table.create_scalar_index("collection", index_type="BITMAP", replace=True)
    logger.info("Created BITMAP index on collection column")


def optimize_table(db: LanceDB) -> None:
    """Compact table data after bulk inserts.

    Merges small data fragments for better query performance.

    Args:
        db: LanceDB connection.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return

    table = db.open_table(TABLE_NAME)
    table.optimize()
    logger.info("Optimized table %s", TABLE_NAME)
