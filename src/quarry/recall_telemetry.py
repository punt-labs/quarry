"""Describe Quarry's recall observations and send them through punt-observe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self, final

from punt_observe.telemetry import (
    BoolAttr,
    CountAttr,
    DurationAttr,
    EnumAttr,
    HashAttr,
    LengthAttr,
    SignalSchema,
)

from quarry.recall_capture import RecallCapture

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from quarry.config import Settings
    from quarry.results import SearchResult

SEARCH = "quarry.daemon.search"
HIT = "quarry.daemon.search_hit"
VERSION = 1
SURFACES = ("cli", "mcp", "http", "plugin", "unknown")
_AGE_BANDS = ((7, "age_0_7d"), (30, "age_7_30d"), (90, "age_30_90d"))
DECAY_LABELS = {
    "age_0_7d": "0-7d",
    "age_7_30d": "7-30d",
    "age_30_90d": "30-90d",
    "age_over_90d": "90d+",
    "age_unknown": "unknown",
}
SCHEMAS = (
    SignalSchema(
        SEARCH,
        VERSION,
        "span",
        {
            "database_hash": HashAttr(),
            "surface": EnumAttr(SURFACES),
            "query_length": LengthAttr(),
            "empty_query_hash": HashAttr(),
            "agent_hash": HashAttr(),
            "memory_query": BoolAttr(),
            "result_count": CountAttr(),
            "latency_ms": DurationAttr(),
        },
    ),
    SignalSchema(
        HIT,
        VERSION,
        "event",
        {"collection_hash": HashAttr(), "decay_band": EnumAttr(tuple(DECAY_LABELS))},
    ),
)


@dataclass(frozen=True, slots=True)
class SearchObservation:
    """Borrow search inputs long enough to derive schema-safe values."""

    query: str
    surface: str
    agent: str
    latency_ms: float
    results: Sequence[SearchResult]


def _decay_band(timestamp: str, now: datetime) -> str:
    try:
        ingested = datetime.fromisoformat(timestamp)
    except ValueError:
        return "age_unknown"
    if ingested.tzinfo is None:
        ingested = ingested.replace(tzinfo=UTC)
    days = (now - ingested).days
    return next((band for upper, band in _AGE_BANDS if days <= upper), "age_over_90d")


@final
class RecallTelemetry:
    """Own Quarry's daemon-lifetime telemetry facade, not its exporter."""

    __slots__ = ("_capture",)

    _capture: RecallCapture

    def __new__(cls, settings: Settings) -> Self:
        """Keep capture dormant until the daemon starts serving."""
        self = super().__new__(cls)
        self._capture = RecallCapture(settings, SCHEMAS)
        return self

    def running(self) -> AbstractContextManager[None]:
        """Start local capture and give queued observations a bounded flush."""
        return self._capture.running()

    @property
    def enabled(self) -> bool:
        """Report whether the daemon has enabled capture."""
        return self._capture.enabled

    def database_hash(self) -> str:
        """Return the database scope used to filter this daemon's insights."""
        return self._capture.database_hash()

    def record(self, observation: SearchObservation) -> None:
        """Submit one recall span and at most one event per returned result."""
        with self._capture.isolate_failure():
            self._record(observation)

    def _record(self, observation: SearchObservation) -> None:
        capture = self._capture.target()
        if capture is None:
            return
        telemetry, scope = capture
        surface = observation.surface
        attrs: dict[str, object] = {
            "database_hash": scope,
            "surface": surface if surface in SURFACES else "unknown",
            "query_length": telemetry.length(observation.query),
            "memory_query": bool(observation.agent),
            "result_count": len(observation.results),
            "latency_ms": observation.latency_ms,
        }
        if not observation.results:
            attrs["empty_query_hash"] = telemetry.hash_id("query:" + observation.query)
        if observation.agent:
            attrs["agent_hash"] = telemetry.hash_id("agent:" + observation.agent)
        now = datetime.now(UTC)
        collections = {
            collection: telemetry.hash_id("collection:" + collection)
            for collection in {result.collection for result in observation.results}
        }
        with telemetry.span(SEARCH, VERSION, **attrs):
            for result in observation.results:
                telemetry.event(
                    HIT,
                    VERSION,
                    collection_hash=collections[result.collection],
                    decay_band=_decay_band(result.ingestion_timestamp, now),
                )
