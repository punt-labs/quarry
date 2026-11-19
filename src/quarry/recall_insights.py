"""Read Quarry's recall aggregates from the shared local export, without writing."""

from __future__ import annotations

import logging
import math
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Self, final

from quarry.api.insights import (
    AgentRecallCount,
    CollectionHitCount,
    DecayBandCount,
    EmptyQueryCount,
    InsightsResponse,
)
from quarry.home_paths import HomeUnresolvedError, QuarryHome
from quarry.recall_telemetry import DECAY_LABELS, HIT, SEARCH, VERSION

if TYPE_CHECKING:
    from quarry.recall_telemetry import RecallTelemetry

logger = logging.getLogger(__name__)

_OVERVIEW = """
    SELECT COUNT(*),
           COALESCE(SUM(json_extract(attributes, '$.result_count') = 0), 0),
           COALESCE(SUM(json_extract(attributes, '$.memory_query')), 0)
    FROM spans
    WHERE name = :search AND schema_version = :version
    AND (:scope IS NULL OR json_extract(attributes, '$.database_hash') = :scope)
"""
_PERCENTILE = """
    SELECT json_extract(attributes, '$.latency_ms') AS latency
    FROM spans
    WHERE name = :search AND schema_version = :version
    AND (:scope IS NULL OR json_extract(attributes, '$.database_hash') = :scope)
    ORDER BY latency LIMIT 1 OFFSET :offset
"""
_COUNTS = """
    WITH searches AS (
        SELECT span_id, attributes
        FROM spans
        WHERE name = :search AND schema_version = :version
        AND (:scope IS NULL OR json_extract(attributes, '$.database_hash') = :scope)
    ), observations AS (
        SELECT attributes FROM searches WHERE NOT :hits
        UNION ALL
        SELECT span_events.attributes
        FROM span_events JOIN searches USING (span_id)
        WHERE :hits AND name = :hit AND schema_version = :version
    )
    SELECT json_extract(attributes, :field) AS label, COUNT(*) AS count
    FROM observations WHERE label IS NOT NULL
    GROUP BY label ORDER BY count DESC, label LIMIT :limit
"""


@final
class RecallInsights:
    """Query the pinned punt-observe SQLite projection for local reports."""

    __slots__ = ("_args", "_conn")

    _conn: sqlite3.Connection
    _args: dict[str, int | str | None]

    def __new__(cls, conn: sqlite3.Connection, database_hash: str | None) -> Self:
        """Bind the signal versions and database scope for one read snapshot."""
        self = super().__new__(cls)
        self._conn = conn
        self._args = {
            "search": SEARCH,
            "hit": HIT,
            "version": VERSION,
            "scope": database_hash,
        }
        return self

    @classmethod
    def read(cls, database_hash: str | None = None) -> InsightsResponse:
        """Read one stable snapshot, or return an empty snapshot before first export.

        A missing scope means the local operator's installation-wide view.

        Raises:
            OSError: The local export cannot be inspected.
            sqlite3.Error: The local export cannot be queried.
        """
        path = QuarryHome.root() / "telemetry.db"
        if not path.exists():
            return InsightsResponse()
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
        ) as conn:
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.execute("BEGIN")
            return cls(conn, database_hash)._snapshot()

    @classmethod
    def for_daemon(cls, recall: RecallTelemetry) -> InsightsResponse:
        """Read scoped insights without accessing a disabled or unavailable store."""
        if not recall.enabled:
            return InsightsResponse.disabled()
        try:
            scope = recall.database_hash()
            return cls.read(scope) if scope else InsightsResponse()
        except (HomeUnresolvedError, OSError, sqlite3.Error):
            logger.debug("Local recall insights are unavailable")
            return InsightsResponse()

    def _snapshot(self) -> InsightsResponse:
        total, empty, memory = self._conn.execute(_OVERVIEW, self._args).fetchone()
        if not total:
            return InsightsResponse()
        return InsightsResponse(
            total_queries=total,
            empty_result_rate=empty / total,
            p50_latency_ms=self._percentile(total, 0.50),
            p95_latency_ms=self._percentile(total, 0.95),
            top_empty_queries=[
                EmptyQueryCount(query_hash=key, count=count)
                for key, count in self._counts("empty_query_hash", limit=10)
            ],
            per_collection_hits=[
                CollectionHitCount(collection_hash=key, hit_count=count)
                for key, count in self._counts("collection_hash", hits=True)
            ],
            per_agent_recall=[
                AgentRecallCount(agent_hash=key, query_count=count)
                for key, count in self._counts("agent_hash")
            ],
            memory_queries=memory,
            knowledge_queries=total - memory,
            hit_decay_bands=[
                DecayBandCount(band=DECAY_LABELS[key], hit_count=count)
                for key, count in self._counts("decay_band", hits=True)
                if key in DECAY_LABELS
            ],
        )

    def _counts(
        self, field: str, *, hits: bool = False, limit: int = 20
    ) -> list[tuple[str, int]]:
        return [
            (str(label), int(count))
            for label, count in self._conn.execute(
                _COUNTS,
                {**self._args, "field": "$." + field, "limit": limit, "hits": hits},
            )
        ]

    def _percentile(self, total: int, fraction: float) -> float:
        offset = math.ceil(fraction * total) - 1
        row = self._conn.execute(
            _PERCENTILE, {**self._args, "offset": offset}
        ).fetchone()
        return round(float(row[0]), 2)
