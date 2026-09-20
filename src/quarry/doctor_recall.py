"""Recall-telemetry doctor check: a one-line summary of recent search activity.

Answers "is the recall loop (DES-055/DES-056) actually being exercised?" without
requiring a running daemon — the query log is a plain SQLite file, so doctor
reads it directly the same way :class:`~quarry.doctor_sync.SyncDiagnostics`
reads the sync registry.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import final

from quarry.query_log import QueryLog
from quarry.results import CheckResult


@final
class RecallDiagnostics:
    """Doctor check over the recall-telemetry query log."""

    __slots__ = ()

    @staticmethod
    def recent_activity(
        telemetry_path: Path, *, telemetry_enabled: bool
    ) -> CheckResult:
        """Summarize total queries and the empty-result rate.

        Informational only (``required=False``): a fresh or disabled telemetry
        store is a normal, healthy state, not a failure.
        """
        result = partial(CheckResult, name="Recall telemetry", required=False)
        if not telemetry_enabled:
            return result(passed=True, message="disabled")
        if not telemetry_path.exists():
            return result(passed=True, message="enabled, no queries recorded yet")
        try:
            summary = RecallDiagnostics._summarize(telemetry_path)
        except Exception as exc:  # noqa: BLE001 -- doctor must degrade, never crash
            return result(passed=False, message=f"check failed: {exc}")
        return result(passed=True, message=summary)

    @staticmethod
    def _summarize(telemetry_path: Path) -> str:
        """Return the one-line summary printed as the ``recent_activity`` message."""
        log = QueryLog(telemetry_path)
        try:
            agg = log.insights.aggregate()
        finally:
            log.close()
        if agg.total_queries == 0:
            return "enabled, no queries recorded yet"
        return (
            f"{agg.total_queries} queries, "
            f"{agg.empty_result_rate * 100:.1f}% empty, "
            f"p50={agg.p50_latency_ms:.1f}ms"
        )
