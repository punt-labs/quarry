"""Read-only aggregations over the recall-telemetry query log.

Split out of ``query_log.py`` (PY-IC-6 single responsibility): reads for
``GET /insights`` are a distinct concern from owning the connection,
recording rows, and scheduling prunes that
:class:`~quarry.query_log.QueryLog` does.
"""

from __future__ import annotations

import math
import sqlite3
import threading
from typing import Self, final

from quarry.query_log_types import CountRow, HitDocRef, RecallAggregate

# Bound the aggregation queries so a very long-lived, never-pruned database
# cannot turn an insights read into an unbounded table scan.
_TOP_EMPTY_LIMIT = 10
_RANKING_LIMIT = 10
_RECENT_HITS_LIMIT = 5000


@final
class QueryLogInsights:
    """Read-only aggregations over the query log, sharing its connection.

    Composed onto :class:`~quarry.query_log.QueryLog` the way ``FileStore``
    and ``CollectionMarkerStore`` compose onto ``SyncRegistry`` — a distinct
    responsibility (reads for ``GET /insights``) sharing one connection with
    the store that owns the writes. Shares :class:`QueryLog`'s lock too: a
    read must never observe another thread's partially-committed rows, since
    ``SearchRoutes.search`` runs on a threadpool and ``/search`` and
    ``/insights`` can execute concurrently against this one connection.
    """

    _conn: sqlite3.Connection
    _lock: threading.Lock

    def __new__(cls, conn: sqlite3.Connection, lock: threading.Lock) -> Self:
        self = super().__new__(cls)
        self._conn = conn
        self._lock = lock
        return self

    def aggregate(self) -> RecallAggregate:
        """Return every SQLite-only insights aggregation in one snapshot."""
        with self._lock:
            total = self._scalar("SELECT COUNT(*) FROM query_events")
            empty = self._scalar(
                "SELECT COUNT(*) FROM query_events WHERE result_count = 0"
            )
            memory = self._scalar(
                "SELECT COUNT(*) FROM query_events WHERE agent_handle != ''"
            )
            p50, p95 = self._latency_percentiles(total)
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
        with self._lock:
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

    def _latency_percentiles(self, total: int) -> tuple[float, float]:
        return self._percentile(0.50, total), self._percentile(0.95, total)

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

    def _percentile(self, pct: float, total: int) -> float:
        """Return the *pct* nearest-rank percentile latency over *total* events.

        ``0.0`` for an empty series -- an insights read on a fresh database must
        report zero latency, not raise on an empty sequence. Nearest-rank is
        ``ceil(pct * n) - 1`` (clamped to ``[0, n - 1]``): plain ``int(pct * n)``
        is off by one whenever ``pct * n`` lands on an integer -- p50 of 4 sorted
        values must read index 1 (the 2nd item), not index 2 (the 3rd). Fetched
        via ``ORDER BY ... LIMIT 1 OFFSET`` rather than loading every latency
        into Python, so an insights read stays a single bounded row fetch
        against a long-lived, never-pruned table instead of an unbounded scan
        and list allocation.
        """
        if total == 0:
            return 0.0
        index = min(total - 1, max(0, math.ceil(pct * total) - 1))
        row = self._conn.execute(
            "SELECT latency_ms FROM query_events ORDER BY latency_ms LIMIT 1 OFFSET ?",
            (index,),
        ).fetchone()
        return round(float(row[0]), 2) if row is not None else 0.0
