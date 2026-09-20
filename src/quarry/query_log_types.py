"""Wire types for the recall-telemetry query log: rows and the insights snapshot.

Split out from ``query_log.py`` (PY-IC-9): these are pure data-transfer shapes
with zero behavior beyond ``RecallAggregate.empty_result_rate``, importable
without pulling in ``sqlite3`` or ``threading`` for a caller (e.g. the daemon
route building a :class:`QueryHit`) that only needs the shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class QueryHit(TypedDict):
    """One ranked result row recorded against a query event.

    A ``TypedDict``, not a dataclass: every field is always supplied by the
    one call site that builds it (the search route, from an already-ranked
    :class:`~quarry.results.SearchResult`), so there is no invariant left for
    a constructor to enforce -- a plain data-transfer shape (PY-OO-4).
    """

    rank: int
    document_name: str
    collection: str
    chunk_index: int
    score: float
    hit_agent_handle: str
    memory_type: str


class QueryEvent(TypedDict):
    """One recorded search: its scrubbed text, filters, and outcome.

    A ``TypedDict`` for the same reason as :class:`QueryHit`: a pure
    data-transfer row built once, fully, by its one call site.
    """

    ts: str
    surface: str
    agent_handle: str
    collection: str
    filters_json: str
    limit_n: int
    latency_ms: float
    result_count: int
    query_scrubbed: str
    query_len: int


class CountRow(TypedDict):
    """One ``(label, count)`` row from a GROUP BY aggregation."""

    label: str
    count: int


class HitDocRef(TypedDict):
    """A recorded hit's document identity, for a caller to join against LanceDB."""

    document_name: str
    collection: str


@dataclass(frozen=True, slots=True)
class RecallAggregate:
    """The SQLite-only aggregations behind ``GET /insights``.

    Excludes the hit decay-band breakdown: that requires joining a hit's
    ``document_name``/``collection`` against LanceDB's ``ingestion_timestamp``,
    which lives outside this engine-free store — the caller (a route handler
    with database access) computes it from
    :meth:`~quarry.query_log.QueryLogInsights.recent_hits`.
    """

    total_queries: int
    empty_queries: int
    p50_latency_ms: float
    p95_latency_ms: float
    top_empty_queries: tuple[CountRow, ...]
    per_collection_hits: tuple[CountRow, ...]
    per_agent_recall: tuple[CountRow, ...]
    memory_queries: int
    knowledge_queries: int

    @property
    def empty_result_rate(self) -> float:
        """Return the fraction of queries with zero hits, or ``0.0`` when none ran."""
        if self.total_queries == 0:
            return 0.0
        return round(self.empty_queries / self.total_queries, 4)
