"""Reciprocal Rank Fusion with optional temporal decay for agent memories."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Self

from quarry.results import SearchResult

logger = logging.getLogger(__name__)

_RowKey = tuple[str, int, int]
_DECAYABLE_TYPES: frozenset[str] = frozenset(
    {"fact", "observation", "opinion", "procedure"}
)


class RrfFusion:
    """Fuse vector and FTS channels by Reciprocal Rank Fusion.

    ``rrf_k`` controls how sharply top ranks dominate (larger ``k`` flattens the
    weighting); ``decay_rate`` applies exponential temporal decay to
    agent-memory rows so recent memories rank higher. ``decay_rate == 0.0`` is
    today's production default and disables decay entirely.
    """

    __slots__ = ("_decay_rate", "_rrf_k")

    _rrf_k: int
    _decay_rate: float

    def __new__(cls, rrf_k: int, decay_rate: float) -> Self:
        self = super().__new__(cls)
        self._rrf_k = rrf_k
        self._decay_rate = decay_rate
        return self

    def fuse(
        self,
        vec_results: list[dict[str, object]],
        fts_results: list[dict[str, object]],
        limit: int,
    ) -> list[SearchResult]:
        """Fuse two ranked channels into one ranked, deduplicated result list."""
        now_ts = datetime.now(tz=UTC).timestamp()
        all_rows: dict[_RowKey, dict[str, object]] = {}
        scores: defaultdict[_RowKey, float] = defaultdict(float)

        # Vector channel first, then FTS -- insertion order is the RRF tie-break.
        for channel in (vec_results, fts_results):
            for rank, row in enumerate(channel):
                key = self._row_key(row)
                scores[key] += self._contribution(row, rank, now_ts)
                if key not in all_rows:
                    all_rows[key] = row

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]

        # Every row carries a real ``_distance``: vector-channel rows from
        # LanceDB, FTS-only rows from ``HybridRetriever`` annotation (quarry-gcnf).
        results = [SearchResult.from_row(all_rows[key]) for key, _ in ranked]
        logger.debug(
            "RRF fusion: %d vector + %d FTS -> %d results",
            len(vec_results),
            len(fts_results),
            len(results),
        )
        return results

    def _contribution(self, row: dict[str, object], rank: int, now_ts: float) -> float:
        """RRF term ``1 / (k + rank)`` for one row, scaled by temporal weight."""
        memory_type = str(row.get("memory_type", ""))
        if self._decay_rate > 0 and memory_type in _DECAYABLE_TYPES:
            ts = row.get("ingestion_timestamp", "")
            weight = self.temporal_weight(ts, now_ts, self._decay_rate)
        else:
            weight = 1.0
        return (1.0 / (self._rrf_k + rank)) * weight

    @staticmethod
    def _row_key(row: dict[str, object]) -> _RowKey:
        """Deduplication key: ``(document_name, chunk_index, page_number)``."""
        return (
            str(row.get("document_name", "")),
            int(str(row.get("chunk_index", 0))),
            int(str(row.get("page_number", 0))),
        )

    @staticmethod
    def temporal_weight(timestamp: object, now_ts: float, decay_rate: float) -> float:
        """Exponential temporal decay weight for a row.

        Returns ``1.0`` when ``decay_rate`` is 0 (no decay) or when the timestamp
        cannot be parsed. Naive datetimes are treated as UTC.
        """
        if decay_rate <= 0:
            return 1.0
        try:
            if isinstance(timestamp, datetime):
                ts = timestamp
            else:
                ts = datetime.fromisoformat(str(timestamp))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            row_ts = ts.timestamp()
        except (ValueError, TypeError, OverflowError):
            return 1.0
        hours = max(0.0, (now_ts - row_ts) / 3600)
        return math.exp(-decay_rate * hours)
