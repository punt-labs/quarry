"""Tests for the recall-telemetry doctor check."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from quarry.doctor_recall import RecallDiagnostics
from quarry.query_log import QueryEvent, QueryLog


def _event(*, result_count: int = 1, latency_ms: float = 10.0) -> QueryEvent:
    return QueryEvent(
        ts=datetime.now(UTC).isoformat(),
        surface="cli",
        agent_handle="",
        collection="default",
        filters_json="{}",
        limit_n=10,
        latency_ms=latency_ms,
        result_count=result_count,
        query_scrubbed="hello world",
        query_len=11,
    )


class TestRecentActivity:
    def test_disabled_reports_disabled(self, tmp_path: Path) -> None:
        result = RecallDiagnostics.recent_activity(
            tmp_path / "telemetry.db", telemetry_enabled=False
        )
        assert result.passed is True
        assert result.required is False
        assert result.message == "disabled"

    def test_enabled_but_no_file_yet_reports_no_queries(self, tmp_path: Path) -> None:
        result = RecallDiagnostics.recent_activity(
            tmp_path / "missing.db", telemetry_enabled=True
        )
        assert result.passed is True
        assert result.message == "enabled, no queries recorded yet"

    def test_enabled_with_empty_store_reports_no_queries(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.db"
        QueryLog(db_path).close()
        result = RecallDiagnostics.recent_activity(db_path, telemetry_enabled=True)
        assert result.passed is True
        assert result.message == "enabled, no queries recorded yet"

    def test_summarizes_seeded_events(self, tmp_path: Path) -> None:
        db_path = tmp_path / "telemetry.db"
        log = QueryLog(db_path)
        log.record(_event(result_count=1, latency_ms=20.0), [])
        log.record(_event(result_count=0, latency_ms=40.0), [])
        log.close()

        result = RecallDiagnostics.recent_activity(db_path, telemetry_enabled=True)
        assert result.passed is True
        assert "2 queries" in result.message
        assert "50.0% empty" in result.message

    def test_read_failure_degrades_to_a_failed_advisory_check(
        self, tmp_path: Path
    ) -> None:
        """A doctor check must degrade, never crash `quarry doctor`."""
        db_path = tmp_path / "telemetry.db"
        QueryLog(db_path).close()
        with patch(
            "quarry.doctor_recall.QueryLog", side_effect=RuntimeError("disk error")
        ):
            result = RecallDiagnostics.recent_activity(db_path, telemetry_enabled=True)
        assert result.passed is False
        assert result.required is False
        assert "check failed" in result.message
