"""Assemble the ``GET /insights`` response from recall-telemetry aggregates.

Split out of ``MetaRoutes`` (PY-IC-6 single responsibility): turning a
:class:`~quarry.query_log_types.RecallAggregate` plus the hit decay-band join
against LanceDB's catalog into the typed :class:`~quarry.api.insights.InsightsResponse`
is one cohesive job, distinct from routing and auth, that ``MetaRoutes.insights``
merely delegates to.
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from quarry.api.insights import (
    AgentRecallCount,
    CollectionHitCount,
    DecayBandCount,
    EmptyQueryCount,
    InsightsResponse,
)

if TYPE_CHECKING:
    from quarry.db import Database
    from quarry.query_log import QueryLog
    from quarry.query_log_types import RecallAggregate

logger = logging.getLogger(__name__)

# Recency bands for the insights hit-decay breakdown, checked in order --
# each band's upper bound in days since the hit's chunk was ingested.
_DECAY_BANDS: tuple[tuple[str, int], ...] = (
    ("0-7d", 7),
    ("7-30d", 30),
    ("30-90d", 90),
    ("90d+", sys.maxsize),
)
_UNKNOWN_BAND = "unknown"


@final
class InsightsBuilder:
    """Assemble one ``InsightsResponse`` snapshot for ``GET /insights``.

    Holds the two things the decay-band join needs together -- the query log
    (for recorded hits) and the live database (for ingestion timestamps) --
    since only a caller with both can compute it (``QueryLogInsights`` is
    engine-free by design; see its docstring).
    """

    __slots__ = ("_database", "_query_log")

    _database: Database
    _query_log: QueryLog

    def __new__(cls, database: Database, query_log: QueryLog) -> Self:
        self = super().__new__(cls)
        self._database = database
        self._query_log = query_log
        return self

    def build(self) -> InsightsResponse:
        """Return the full recall-telemetry snapshot as a typed response."""
        agg = self._query_log.insights.aggregate()
        return InsightsResponse(
            telemetry_enabled=True,
            total_queries=agg.total_queries,
            empty_result_rate=agg.empty_result_rate,
            p50_latency_ms=agg.p50_latency_ms,
            p95_latency_ms=agg.p95_latency_ms,
            top_empty_queries=self._empty_query_rows(agg),
            per_collection_hits=self._collection_hit_rows(agg),
            per_agent_recall=self._agent_recall_rows(agg),
            memory_queries=agg.memory_queries,
            knowledge_queries=agg.knowledge_queries,
            hit_decay_bands=self._decay_band_rows(),
        )

    @staticmethod
    def _empty_query_rows(agg: RecallAggregate) -> list[EmptyQueryCount]:
        return [
            EmptyQueryCount(query_scrubbed=r["label"], count=r["count"])
            for r in agg.top_empty_queries
        ]

    @staticmethod
    def _collection_hit_rows(agg: RecallAggregate) -> list[CollectionHitCount]:
        return [
            CollectionHitCount(collection=r["label"], hit_count=r["count"])
            for r in agg.per_collection_hits
        ]

    @staticmethod
    def _agent_recall_rows(agg: RecallAggregate) -> list[AgentRecallCount]:
        return [
            AgentRecallCount(agent_handle=r["label"], query_count=r["count"])
            for r in agg.per_agent_recall
        ]

    def _decay_band_rows(self) -> list[DecayBandCount]:
        return [
            DecayBandCount(band=band, hit_count=count)
            for band, count in self._hit_decay_bands().items()
        ]

    def _hit_decay_bands(self) -> dict[str, int]:
        """Bucket recent hits by the age of the document chunk they came from."""
        refs = self._query_log.insights.recent_hits()
        if not refs:
            return {}
        ingested_at = self._document_ingestion_index()
        now = datetime.now(UTC)
        counts: Counter[str] = Counter()
        for ref in refs:
            ts = ingested_at.get((ref["collection"], ref["document_name"]))
            counts[self._band(ts, now) if ts is not None else _UNKNOWN_BAND] += 1
        return dict(counts)

    def _document_ingestion_index(self) -> dict[tuple[str, str], datetime]:
        """Map ``(collection, document_name)`` to its ingestion timestamp.

        A document whose stored ``ingestion_timestamp`` doesn't parse is
        logged and skipped -- it falls back to the "unknown" band rather than
        silently vanishing from the breakdown with no trace of why. A naive
        timestamp is treated as UTC (matching
        ``retrieval.fusion.RecencyFuser.temporal_weight``) rather than left
        naive: :meth:`_band` subtracts it from an aware ``datetime.now(UTC)``,
        and naive-minus-aware raises ``TypeError``.
        """
        index: dict[tuple[str, str], datetime] = {}
        for doc in self._database.catalog.list_documents():
            try:
                ts = datetime.fromisoformat(doc["ingestion_timestamp"])
            except ValueError:
                logger.warning(
                    "Skipping document with malformed ingestion_timestamp: "
                    "collection=%s document_name=%s",
                    doc.get("collection"),
                    doc.get("document_name"),
                )
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            index[(doc["collection"], doc["document_name"])] = ts
        return index

    @staticmethod
    def _band(ingested_at: datetime, now: datetime) -> str:
        """Return the recency-band label *ingested_at* falls into as of *now*."""
        age_days = (now - ingested_at).days
        for label, max_days in _DECAY_BANDS:
            if age_days <= max_days:
                return label
        return _DECAY_BANDS[-1][0]
