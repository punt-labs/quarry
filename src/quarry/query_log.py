"""Local SQLite store for recall telemetry: query events and their ranked hits.

Mirrors :class:`~quarry.sync_registry.SyncRegistry`'s connection lifecycle (WAL,
a busy timeout, ``check_same_thread=False``) so the daemon holds one
daemon-lifetime connection per database, built lazily via :func:`get_query_log`
and cached for the process — the same "one resident connection" shape as
``QuarryResources.database``, without adding a new field to ``DaemonContext``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Self, final

from quarry.config import Settings
from quarry.query_log_schema import QueryLogSchema


@dataclass(frozen=True, slots=True)
class QueryHit:
    """One ranked result row recorded against a query event."""

    rank: int
    document_name: str
    collection: str
    chunk_index: int
    score: float
    hit_agent_handle: str = ""
    memory_type: str = ""


@dataclass(frozen=True, slots=True)
class QueryEvent:
    """One recorded search: its scrubbed text, filters, and outcome."""

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


@dataclass(frozen=True, slots=True)
class CountRow:
    """One ``(label, count)`` row from a GROUP BY aggregation."""

    label: str
    count: int


@dataclass(frozen=True, slots=True)
class RecallAggregate:
    """The SQLite-only aggregations behind ``GET /insights``.

    Excludes the hit decay-band breakdown: that requires joining a hit's
    ``document_name``/``collection`` against LanceDB's ``ingestion_timestamp``,
    which lives outside this engine-free store — the caller (a route handler
    with database access) computes it from :meth:`QueryLogInsights.recent_hits`.
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


@dataclass(frozen=True, slots=True)
class HitDocRef:
    """A recorded hit's document identity, for a caller to join against LanceDB."""

    document_name: str
    collection: str


# Bound the aggregation queries so a very long-lived, never-pruned database
# cannot turn an insights read into an unbounded table scan.
_TOP_EMPTY_LIMIT = 10
_RANKING_LIMIT = 10
_RECENT_HITS_LIMIT = 5000


@final
class QueryLogInsights:
    """Read-only aggregations over the query log, sharing its connection.

    Composed onto :class:`QueryLog` the way ``FileStore`` and
    ``CollectionMarkerStore`` compose onto ``SyncRegistry`` — a distinct
    responsibility (reads for ``GET /insights``) sharing one connection with the
    store that owns the writes.
    """

    _conn: sqlite3.Connection

    def __new__(cls, conn: sqlite3.Connection) -> Self:
        self = super().__new__(cls)
        self._conn = conn
        return self

    def aggregate(self) -> RecallAggregate:
        """Return every SQLite-only insights aggregation in one snapshot."""
        total = self._scalar("SELECT COUNT(*) FROM query_events")
        empty = self._scalar("SELECT COUNT(*) FROM query_events WHERE result_count = 0")
        memory = self._scalar(
            "SELECT COUNT(*) FROM query_events WHERE agent_handle != ''"
        )
        p50, p95 = self._latency_percentiles()
        return RecallAggregate(
            total_queries=total,
            empty_queries=empty,
            p50_latency_ms=p50,
            p95_latency_ms=p95,
            top_empty_queries=self._top_empty_queries(),
            per_collection_hits=self._per_collection_hits(),
            per_agent_recall=self._per_agent_recall(),
            memory_queries=memory,
            knowledge_queries=total - memory,
        )

    def recent_hits(self, limit: int = _RECENT_HITS_LIMIT) -> list[HitDocRef]:
        """Return the most recent hits' document identity, newest first.

        Bounded by *limit* so a caller computing a decay-band breakdown reads a
        fixed-size window rather than the full (unbounded) hit history.
        """
        rows = self._conn.execute(
            "SELECT h.document_name, h.collection FROM query_hits h "
            "JOIN query_events e ON e.id = h.query_event_id "
            "ORDER BY e.ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [HitDocRef(document_name=r[0], collection=r[1]) for r in rows]

    def _scalar(self, sql: str) -> int:
        row = self._conn.execute(sql).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _latency_percentiles(self) -> tuple[float, float]:
        rows = self._conn.execute(
            "SELECT latency_ms FROM query_events ORDER BY latency_ms"
        ).fetchall()
        values = [float(r[0]) for r in rows]
        return self._percentile(values, 0.50), self._percentile(values, 0.95)

    def _top_empty_queries(self) -> tuple[CountRow, ...]:
        rows = self._conn.execute(
            "SELECT query_scrubbed, COUNT(*) AS c FROM query_events "
            "WHERE result_count = 0 GROUP BY query_scrubbed "
            "ORDER BY c DESC LIMIT ?",
            (_TOP_EMPTY_LIMIT,),
        ).fetchall()
        return tuple(CountRow(label=r[0], count=r[1]) for r in rows)

    def _per_collection_hits(self) -> tuple[CountRow, ...]:
        rows = self._conn.execute(
            "SELECT collection, COUNT(*) AS c FROM query_hits "
            "GROUP BY collection ORDER BY c DESC LIMIT ?",
            (_RANKING_LIMIT,),
        ).fetchall()
        return tuple(CountRow(label=r[0], count=r[1]) for r in rows)

    def _per_agent_recall(self) -> tuple[CountRow, ...]:
        rows = self._conn.execute(
            "SELECT agent_handle, COUNT(*) AS c FROM query_events "
            "WHERE agent_handle != '' GROUP BY agent_handle "
            "ORDER BY c DESC LIMIT ?",
            (_RANKING_LIMIT,),
        ).fetchall()
        return tuple(CountRow(label=r[0], count=r[1]) for r in rows)

    @staticmethod
    def _percentile(sorted_values: list[float], pct: float) -> float:
        """Return the *pct* percentile (nearest-rank) of *sorted_values*.

        ``0.0`` for an empty series -- an insights read on a fresh database must
        report zero latency, not raise on an empty sequence.
        """
        if not sorted_values:
            return 0.0
        index = min(len(sorted_values) - 1, int(pct * len(sorted_values)))
        return round(sorted_values[index], 2)


@final
class QueryLog:
    """Own the query-log SQLite connection: record, prune, and expose insights.

    A daemon-lifetime connection built lazily via :func:`get_query_log`. Every
    write is a boundary I/O side effect the search route swallows on failure
    (PY-EH boundary I/O) -- this store's job is to never be the reason a search
    fails, so its own methods stay simple and let sqlite3 errors propagate to
    that one guarded call site rather than swallowing them here.
    """

    _conn: sqlite3.Connection
    _insights: QueryLogInsights

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: background ingest/task threads share the
        # daemon's one connection with the request-handling thread, matching
        # SyncRegistry's rationale.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self._ensure_schema()
        except Exception:
            self._conn.close()
            raise
        self._insights = QueryLogInsights(self._conn)
        return self

    @property
    def insights(self) -> QueryLogInsights:
        """Return the read-only aggregation surface sharing this connection."""
        return self._insights

    def _ensure_schema(self) -> None:
        """Set connection pragmas, create tables, and apply migrations."""
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait up to 5 s for a contended write lock, matching SyncRegistry.
        self._conn.execute("PRAGMA busy_timeout=5000")
        schema = QueryLogSchema(self._conn)
        schema.initialize()
        schema.migrate()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def record(self, event: QueryEvent, hits: Sequence[QueryHit]) -> None:
        """Insert one ``query_events`` row plus one ``query_hits`` row per hit."""
        cursor = self._conn.execute(
            "INSERT INTO query_events "
            "(ts, surface, agent_handle, collection, filters_json, limit_n, "
            "latency_ms, result_count, query_scrubbed, query_len) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.ts,
                event.surface,
                event.agent_handle,
                event.collection,
                event.filters_json,
                event.limit_n,
                event.latency_ms,
                event.result_count,
                event.query_scrubbed,
                event.query_len,
            ),
        )
        event_id = cursor.lastrowid
        if hits:
            self._conn.executemany(
                "INSERT INTO query_hits "
                "(query_event_id, rank, document_name, collection, chunk_index, "
                "score, hit_agent_handle, memory_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        event_id,
                        hit.rank,
                        hit.document_name,
                        hit.collection,
                        hit.chunk_index,
                        hit.score,
                        hit.hit_agent_handle,
                        hit.memory_type,
                    )
                    for hit in hits
                ],
            )
        self._conn.commit()

    def prune(self, retention_days: int) -> int:
        """Delete events and hits older than *retention_days*; return the count."""
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        stale_ids = [
            row[0]
            for row in self._conn.execute(
                "SELECT id FROM query_events WHERE ts < ?", (cutoff,)
            )
        ]
        if not stale_ids:
            return 0
        # One row per statement rather than a dynamic IN-clause: a fixed
        # parameterized statement needs no f-string SQL at all, so there is
        # nothing for a query-count-scale prune batch to lose by avoiding it.
        rows = [(i,) for i in stale_ids]
        self._conn.executemany("DELETE FROM query_hits WHERE query_event_id = ?", rows)
        self._conn.executemany("DELETE FROM query_events WHERE id = ?", rows)
        self._conn.commit()
        return len(stale_ids)


@cache
def get_query_log(path: Path) -> QueryLog:
    """Return the process-cached, daemon-lifetime :class:`QueryLog` for *path*.

    One entry per resolved path, so every route handler in the process shares the
    same connection without a new field on ``DaemonContext`` -- the same "build
    once, cache for the process" shape as ``ProviderSelection.display_cached()``.
    Pruned once here, at first construction, which approximates "on daemon
    start" for the process that first touches telemetry (search or insights);
    :meth:`QueryLog.prune` runs again opportunistically from the search route.
    """
    log = QueryLog(path)
    log.prune(Settings.load().telemetry_retention_days)
    return log
