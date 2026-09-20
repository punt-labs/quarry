"""The insights contract: recall-telemetry aggregations for ``GET /insights``."""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel


class EmptyQueryCount(TypedDict):
    """One scrubbed query text that returned zero hits, and how often."""

    query_scrubbed: str
    count: int


class CollectionHitCount(TypedDict):
    """One collection's share of recorded hits."""

    collection: str
    hit_count: int


class AgentRecallCount(TypedDict):
    """One agent handle's query volume."""

    agent_handle: str
    query_count: int


class DecayBandCount(TypedDict):
    """One recency band's share of recorded hits, by chunk ingestion age."""

    band: str
    hit_count: int


class InsightsResponse(BaseModel):
    """The aggregate recall-telemetry snapshot behind ``quarry insights``."""

    telemetry_enabled: bool
    total_queries: int
    empty_result_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    top_empty_queries: list[EmptyQueryCount]
    per_collection_hits: list[CollectionHitCount]
    per_agent_recall: list[AgentRecallCount]
    memory_queries: int
    knowledge_queries: int
    hit_decay_bands: list[DecayBandCount]
