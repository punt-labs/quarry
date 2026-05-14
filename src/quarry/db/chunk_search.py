"""Chunk search: vector similarity, hybrid BM25+vector, and RRF fusion."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import TYPE_CHECKING, Self, cast

from quarry._sql import escape_sql
from quarry.db.schema import TABLE_NAME, SchemaManager
from quarry.results import SearchResult
from quarry.types import LanceDB

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)

# RRF constant -- controls how much top-ranked results dominate.
_RRF_K = 60

_RowKey = tuple[str, int, int]
_DECAYABLE_TYPES: frozenset[str] = frozenset(
    {"fact", "observation", "opinion", "procedure"}
)


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
        parts.append(f"document_name = '{escape_sql(document_filter)}'")
    if collection_filter:
        parts.append(f"collection = '{escape_sql(collection_filter)}'")
    if page_type_filter:
        parts.append(f"page_type = '{escape_sql(page_type_filter)}'")
    if source_format_filter:
        parts.append(f"source_format = '{escape_sql(source_format_filter)}'")
    if agent_handle_filter:
        parts.append(f"agent_handle = '{escape_sql(agent_handle_filter)}'")
    if memory_type_filter:
        parts.append(f"memory_type = '{escape_sql(memory_type_filter)}'")
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


class ChunkSearch:
    """Search chunks by vector similarity and hybrid BM25+vector with RRF."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    def vector_search(
        self,
        query_vector: NDArray[np.float32],
        limit: int = 10,
        document_filter: str | None = None,
        collection_filter: str | None = None,
        page_type_filter: str | None = None,
        source_format_filter: str | None = None,
    ) -> list[SearchResult]:
        """Search for similar chunks using vector similarity."""
        logger.debug(
            "Search: limit=%d, document_filter=%s, collection_filter=%s, "
            "page_type_filter=%s, source_format_filter=%s",
            limit,
            document_filter,
            collection_filter,
            page_type_filter,
            source_format_filter,
        )

        if TABLE_NAME not in self._db.list_tables().tables:
            logger.debug("Search: table %s not found, returning empty", TABLE_NAME)
            return []

        table = self._db.open_table(TABLE_NAME)
        query = table.search(query_vector.tolist()).limit(limit)

        predicates: list[str] = []
        if document_filter:
            predicates.append(f"document_name = '{escape_sql(document_filter)}'")
        if collection_filter:
            predicates.append(f"collection = '{escape_sql(collection_filter)}'")
        if page_type_filter:
            predicates.append(f"page_type = '{escape_sql(page_type_filter)}'")
        if source_format_filter:
            predicates.append(f"source_format = '{escape_sql(source_format_filter)}'")
        if predicates:
            query = query.where(" AND ".join(predicates))

        results = query.to_list()
        logger.debug("Search: %d results returned", len(results))
        return cast("list[SearchResult]", results)

    def hybrid_search(
        self,
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
        """Multi-channel search: vector similarity + BM25 FTS, fused with RRF."""
        if TABLE_NAME not in self._db.list_tables().tables:
            return []

        table = self._db.open_table(TABLE_NAME)
        mgr = SchemaManager(self._db)
        mgr.migrate(table)
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
            logger.warning(
                "FTS search failed, using vector-only results", exc_info=True
            )

        return _fuse_rrf(vec_results, fts_results, limit, decay_rate)
