"""The insights contract: recall-telemetry aggregations for ``GET /insights``."""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel


class EmptyQueryCount(TypedDict):
    """One salted query hash that returned zero hits, and how often."""

    query_hash: str
    count: int


class CollectionHitCount(TypedDict):
    """One hashed collection's share of recorded hits."""

    collection_hash: str
    hit_count: int


class AgentRecallCount(TypedDict):
    """One hashed agent handle's query volume."""

    agent_hash: str
    query_count: int


class DecayBandCount(TypedDict):
    """One recency band's share of recorded hits, by chunk ingestion age."""

    band: str
    hit_count: int


class InsightsResponse(BaseModel):
    """The aggregate recall-telemetry snapshot behind ``quarry insights``."""

    telemetry_enabled: bool = True
    total_queries: int = 0
    empty_result_rate: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    top_empty_queries: list[EmptyQueryCount] = []
    per_collection_hits: list[CollectionHitCount] = []
    per_agent_recall: list[AgentRecallCount] = []
    memory_queries: int = 0
    knowledge_queries: int = 0
    hit_decay_bands: list[DecayBandCount] = []

    @classmethod
    def disabled(cls) -> InsightsResponse:
        """Return the all-zero snapshot without opening any store."""
        return cls(telemetry_enabled=False)
