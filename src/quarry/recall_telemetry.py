"""Describe Quarry's recall observations and send them through punt-observe."""

from __future__ import annotations

import logging
from contextlib import contextmanager
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
    NoopTelemetry,
    SignalSchema,
    TelemetryConfig,
    open_telemetry,
)

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from punt_observe.telemetry import IdentifierHash, Telemetry

    from quarry.config import Settings
    from quarry.results import SearchResult

logger = logging.getLogger(__name__)

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

    __slots__ = ("_scope", "_settings", "_telemetry")

    _settings: Settings
    _telemetry: Telemetry | None
    _scope: IdentifierHash | None

    def __new__(cls, settings: Settings) -> Self:
        """Keep capture dormant until the daemon starts serving."""
        self = super().__new__(cls)
        self._settings = settings
        self._telemetry = None
        self._scope = None
        return self

    @contextmanager
    def running(self) -> Generator[None]:
        """Start local capture and give queued observations a bounded flush."""
        config = TelemetryConfig(enabled=self._settings.telemetry_enabled)
        telemetry = open_telemetry("quarry", SCHEMAS, config)
        if not isinstance(telemetry, NoopTelemetry):
            self._telemetry = telemetry
            self._scope = telemetry.hash_id(
                f"database:{self._settings.lancedb_path.resolve()}"
            )
        try:
            yield
        finally:
            self._telemetry = self._scope = None
            telemetry.shutdown()

    @property
    def enabled(self) -> bool:
        """Report whether the daemon has enabled capture."""
        return self._telemetry is not None

    def database_hash(self) -> str:
        """Return the database scope used to filter this daemon's insights."""
        return self._scope.value if self._scope is not None else ""

    def record(self, observation: SearchObservation) -> None:
        """Submit one recall span and at most one event per returned result."""
        try:
            self._record(observation)
        except Exception as exc:
            logger.warning(
                "Recall telemetry capture failed", exc_info=RedactedFailure.of(exc)
            )

    def _record(self, observation: SearchObservation) -> None:
        telemetry = self._telemetry
        if telemetry is None or self._scope is None or not self._scope.value:
            return
        surface = observation.surface
        attrs: dict[str, object] = {
            "database_hash": self._scope,
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
