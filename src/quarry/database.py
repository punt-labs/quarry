"""LanceDB operations: insert chunks, search, list documents and collections."""

from __future__ import annotations

import logging
import math
import threading
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pyarrow as pa

from quarry.models import Chunk
from quarry.results import (
    CollectionSummary,
    DatabaseSummary,
    DocumentSummary,
    SearchResult,
)
from quarry.types import LanceDB, LanceTable

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

TABLE_NAME = "chunks"
_table_lock = threading.Lock()

# table.search() without a vector defaults to a limited scan (typically 10
# rows).  Non-vector filtered queries must set an explicit limit large enough
# to cover the full table so the WHERE clause is evaluated against every row.
_FULL_SCAN_LIMIT = 1_000_000


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
            pa.field("page_type", pa.utf8()),
            pa.field("source_format", pa.utf8()),
            pa.field("ingestion_timestamp", pa.timestamp("us", tz="UTC")),
            pa.field("agent_handle", pa.utf8()),
            pa.field("memory_type", pa.utf8()),
            pa.field("summary", pa.utf8()),
        ]
    )


# Columns added by schema migration, with their SQL default expressions.
_MIGRATION_COLUMNS: dict[str, str] = {
    "agent_handle": "''",
    "memory_type": "''",
    "summary": "''",
}


def _migrate_schema(table: LanceTable) -> None:
    """Add missing columns to an existing table.

    Idempotent — checks the table schema before adding each column.
    Called on every table open so pre-existing databases gain new columns
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


def _ensure_fts_index(table: LanceTable) -> None:
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


def ensure_schema(db: LanceDB) -> None:
    """Run schema migration and FTS index creation on an existing table.

    Safe to call at application startup. No-op if the table does not exist.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return
    table = db.open_table(TABLE_NAME)
    _migrate_schema(table)
    _ensure_fts_index(table)


def get_db(db_path: Path) -> LanceDB:
    import lancedb  # noqa: PLC0415

    db_path.mkdir(parents=True, exist_ok=True)
    return cast("LanceDB", lancedb.connect(str(db_path)))  # type: ignore[attr-defined]


def _get_or_create_table(
    db: LanceDB,
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
    if TABLE_NAME in db.list_tables().tables:
        table = db.open_table(TABLE_NAME)
        _migrate_schema(table)
        _ensure_fts_index(table)
        return table
    with _table_lock:
        if TABLE_NAME in db.list_tables().tables:
            table = db.open_table(TABLE_NAME)
            _migrate_schema(table)
            _ensure_fts_index(table)
            return table
        table = db.create_table(TABLE_NAME, data=records, schema=_schema())
        _ensure_fts_index(table)
        return None


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

    table = _get_or_create_table(db, records)
    if table is not None:
        table.add(records)

    logger.info("Inserted %d chunks into %s", len(records), TABLE_NAME)
    return len(records)


def search(
    db: LanceDB,
    query_vector: NDArray[np.float32],
    limit: int = 10,
    document_filter: str | None = None,
    collection_filter: str | None = None,
    page_type_filter: str | None = None,
    source_format_filter: str | None = None,
) -> list[SearchResult]:
    """Search for similar chunks using vector similarity.

    Args:
        db: LanceDB connection.
        query_vector: Query embedding vector.
        limit: Maximum results to return.
        document_filter: Optional document name filter (exact match).
        collection_filter: Optional collection name filter (pre-filter).
        page_type_filter: Optional content type filter (text, code, etc.).
        source_format_filter: Optional source format filter (.pdf, .py, etc.).

    Returns:
        List of result dicts with text, metadata, and _distance.
    """
    logger.debug(
        "Search: limit=%d, document_filter=%s, collection_filter=%s, "
        "page_type_filter=%s, source_format_filter=%s",
        limit,
        document_filter,
        collection_filter,
        page_type_filter,
        source_format_filter,
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
    if page_type_filter:
        predicates.append(f"page_type = '{_escape_sql(page_type_filter)}'")
    if source_format_filter:
        predicates.append(f"source_format = '{_escape_sql(source_format_filter)}'")
    if predicates:
        query = query.where(" AND ".join(predicates))

    results = query.to_list()
    logger.debug("Search: %d results returned", len(results))
    return cast("list[SearchResult]", results)


# RRF constant — controls how much top-ranked results dominate.
_RRF_K = 60


def _build_predicates(
    document_filter: str | None,
    collection_filter: str | None,
    page_type_filter: str | None,
    source_format_filter: str | None,
    agent_handle_filter: str | None,
    memory_type_filter: str | None,
) -> str | None:
    """Build a SQL WHERE clause from optional filters."""
    parts: list[str] = []
    if document_filter:
        parts.append(f"document_name = '{_escape_sql(document_filter)}'")
    if collection_filter:
        parts.append(f"collection = '{_escape_sql(collection_filter)}'")
    if page_type_filter:
        parts.append(f"page_type = '{_escape_sql(page_type_filter)}'")
    if source_format_filter:
        parts.append(f"source_format = '{_escape_sql(source_format_filter)}'")
    if agent_handle_filter:
        parts.append(f"agent_handle = '{_escape_sql(agent_handle_filter)}'")
    if memory_type_filter:
        parts.append(f"memory_type = '{_escape_sql(memory_type_filter)}'")
    return " AND ".join(parts) if parts else None


def _temporal_weight(
    timestamp: object,
    now_ts: float,
    decay_rate: float,
) -> float:
    """Compute exponential temporal decay weight for a row.

    Returns 1.0 when decay_rate is 0 (no decay) or when the timestamp
    cannot be parsed.  Naive datetimes are treated as UTC.
    """
    if decay_rate <= 0:
        return 1.0
    from datetime import UTC, datetime  # noqa: PLC0415

    try:
        if isinstance(timestamp, datetime):
            ts = timestamp
        else:
            ts = datetime.fromisoformat(str(timestamp))
        # Treat naive datetimes as UTC.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        row_ts = ts.timestamp()
    except (ValueError, TypeError, OverflowError):
        return 1.0
    hours = max(0.0, (now_ts - row_ts) / 3600)
    return math.exp(-decay_rate * hours)


_RowKey = tuple[str, int, int]
_DECAYABLE_TYPES: frozenset[str] = frozenset(
    {"fact", "observation", "opinion", "procedure"}
)


def _row_key(row: dict[str, object]) -> _RowKey:
    """Deduplication key for a chunk row."""
    return (
        str(row.get("document_name", "")),
        int(str(row.get("chunk_index", 0))),
        int(str(row.get("page_number", 0))),
    )


def _fuse_rrf(
    vec_results: list[dict[str, object]],
    fts_results: list[dict[str, object]],
    limit: int,
    decay_rate: float,
) -> list[SearchResult]:
    """Fuse vector and FTS results using Reciprocal Rank Fusion."""
    from datetime import UTC, datetime  # noqa: PLC0415

    now_ts = datetime.now(tz=UTC).timestamp()
    all_rows: dict[_RowKey, dict[str, object]] = {}
    scores: defaultdict[_RowKey, float] = defaultdict(float)

    for rank, row in enumerate(vec_results):
        key = _row_key(row)
        memory_type = str(row.get("memory_type", ""))
        if decay_rate > 0 and memory_type in _DECAYABLE_TYPES:
            ts = row.get("ingestion_timestamp", "")
            weight = _temporal_weight(ts, now_ts, decay_rate)
        else:
            weight = 1.0
        scores[key] += (1.0 / (_RRF_K + rank)) * weight
        if key not in all_rows:
            all_rows[key] = row

    for rank, row in enumerate(fts_results):
        key = _row_key(row)
        memory_type = str(row.get("memory_type", ""))
        if decay_rate > 0 and memory_type in _DECAYABLE_TYPES:
            ts = row.get("ingestion_timestamp", "")
            weight = _temporal_weight(ts, now_ts, decay_rate)
        else:
            weight = 1.0
        scores[key] += (1.0 / (_RRF_K + rank)) * weight
        if key not in all_rows:
            all_rows[key] = row

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]

    results: list[SearchResult] = []
    for key, score in ranked:
        row = all_rows[key]
        # Preserve the original vector distance when available; FTS-only
        # results (no vector channel hit) get _distance 0.0.
        if "_distance" not in row:
            row["_distance"] = 0.0
        row["rrf_score"] = score
        results.append(cast("SearchResult", row))

    logger.debug(
        "RRF fusion: %d vector + %d FTS → %d results",
        len(vec_results),
        len(fts_results),
        len(results),
    )
    return results


def hybrid_search(
    db: LanceDB,
    query_text: str,
    query_vector: NDArray[np.float32],
    limit: int = 10,
    document_filter: str | None = None,
    collection_filter: str | None = None,
    page_type_filter: str | None = None,
    source_format_filter: str | None = None,
    agent_handle_filter: str | None = None,
    memory_type_filter: str | None = None,
    decay_rate: float = 0.0,
) -> list[SearchResult]:
    """Multi-channel search: vector similarity + BM25 FTS, fused with RRF.

    Runs two channels in parallel:
      1. Vector similarity (semantic)
      2. Full-text search via Tantivy (keyword/BM25)

    Results are combined using Reciprocal Rank Fusion (RRF) with optional
    exponential temporal decay weighting.

    Args:
        db: LanceDB connection.
        query_text: Raw query string (used for FTS channel).
        query_vector: Query embedding vector (used for vector channel).
        limit: Maximum results to return.
        document_filter: Optional document name filter.
        collection_filter: Optional collection filter.
        page_type_filter: Optional content type filter.
        source_format_filter: Optional source format filter.
        agent_handle_filter: Optional agent handle filter.
        memory_type_filter: Optional memory type filter.
        decay_rate: Temporal decay rate per hour. 0 disables decay.

    Returns:
        List of SearchResult dicts, ranked by fused RRF score.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return []

    table = db.open_table(TABLE_NAME)
    _migrate_schema(table)
    predicate = _build_predicates(
        document_filter,
        collection_filter,
        page_type_filter,
        source_format_filter,
        agent_handle_filter,
        memory_type_filter,
    )
    fetch_limit = limit * 3  # over-fetch for better fusion

    # Channel 1: Vector similarity
    vec_query = table.search(query_vector.tolist()).limit(fetch_limit)
    if predicate:
        vec_query = vec_query.where(predicate)
    vec_results = vec_query.to_list()

    # Channel 2: Full-text search (BM25)
    fts_results: list[dict[str, object]] = []
    try:
        fts_query = table.search(query_text, query_type="fts").limit(fetch_limit)
        if predicate:
            fts_query = fts_query.where(predicate)
        fts_results = fts_query.to_list()
    except (OSError, ValueError, RuntimeError):
        # FTS index may not exist on legacy tables; fall back to vector-only
        logger.warning("FTS search failed, using vector-only results", exc_info=True)

    return _fuse_rrf(vec_results, fts_results, limit, decay_rate)


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
        table.search()
        .where(predicate)
        .limit(_FULL_SCAN_LIMIT)
        .select(["page_raw_text"])
        .to_list()
    )

    if not results:
        logger.debug("get_page_text: no results found")
        return None
    return str(results[0]["page_raw_text"])


def list_documents(
    db: LanceDB,
    collection_filter: str | None = None,
) -> list[DocumentSummary]:
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
    query = (
        table.search()
        .limit(_FULL_SCAN_LIMIT)
        .select(
            [
                "document_name",
                "document_path",
                "collection",
                "total_pages",
                "page_number",
                "ingestion_timestamp",
            ]
        )
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

    docs: list[DocumentSummary] = []
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


def list_collections(db: LanceDB) -> list[CollectionSummary]:
    """List all collections with document and chunk counts.

    Returns:
        List of dicts with collection, document_count, chunk_count.
    """
    if TABLE_NAME not in db.list_tables().tables:
        return []

    table = db.open_table(TABLE_NAME)
    rows = (
        table.search()
        .limit(_FULL_SCAN_LIMIT)
        .select(["collection", "document_name"])
        .to_list()
    )
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


def format_size(size_bytes: int) -> str:
    """Human-readable size string from byte count."""
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes == 1:
        return "1 byte"
    return f"{size_bytes} bytes"


def discover_databases(root: Path) -> list[DatabaseSummary]:
    """Scan *root* for named databases and return structured summaries."""
    results: list[DatabaseSummary] = []
    if not root.exists():
        return results
    for entry in sorted(root.iterdir()):
        lance_dir = entry / "lancedb"
        if not entry.is_dir() or not lance_dir.exists():
            continue
        db = get_db(lance_dir)
        docs = list_documents(db)
        size_bytes = sum(f.stat().st_size for f in lance_dir.rglob("*") if f.is_file())
        results.append(
            DatabaseSummary(
                name=entry.name,
                document_count=len(docs),
                size_bytes=size_bytes,
                size_description=format_size(size_bytes),
            )
        )
    return results
