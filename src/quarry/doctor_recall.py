"""Summarize local recall activity without starting a telemetry provider."""

from __future__ import annotations

import sqlite3
from functools import partial
from typing import TYPE_CHECKING, final

from punt_observe.telemetry import TelemetryConfig
from punt_observe.telemetry.config import resolve

from quarry.home_paths import HomeUnresolvedError
from quarry.recall_insights import RecallInsights
from quarry.results import CheckResult

if TYPE_CHECKING:
    from quarry.config import Settings


@final
class RecallDiagnostics:
    """Read the installation-wide recall summary for doctor."""

    @staticmethod
    def recent_activity(settings: Settings) -> CheckResult:
        """Return a non-required recall check without starting capture."""
        result = partial(CheckResult, name="Recall telemetry", required=False)
        config = TelemetryConfig(enabled=settings.telemetry_enabled)
        if not resolve("quarry", config).enabled:
            return result(passed=True, message="disabled")
        try:
            return result(passed=True, message=RecallDiagnostics._summary())
        except (HomeUnresolvedError, OSError, sqlite3.Error):
            return result(passed=False, message="local telemetry unavailable")

    @staticmethod
    def _summary() -> str:
        agg = RecallInsights.read()
        if agg.total_queries == 0:
            return "enabled, no queries recorded yet"
        return (
            f"{agg.total_queries} queries, "
            f"{agg.empty_result_rate * 100:.1f}% empty, "
            f"p50={agg.p50_latency_ms:.1f}ms"
        )
