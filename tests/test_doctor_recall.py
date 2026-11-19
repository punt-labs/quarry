"""Tests for the advisory recall check over the shared telemetry export."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.config import Settings
from quarry.doctor_recall import RecallDiagnostics
from quarry.recall_telemetry import RecallTelemetry, SearchObservation

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


def test_disabled_reports_disabled() -> None:
    report = RecallDiagnostics.recent_activity(Settings(telemetry_enabled=False))
    assert report.passed and not report.required
    assert report.message == "disabled"


def test_doctor_reads_an_existing_export_without_opening_one(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings()
    assert "no queries" in RecallDiagnostics.recent_activity(settings).message
    assert not (tmp_path / ".punt-labs").exists()

    telemetry = RecallTelemetry(settings)
    with telemetry.running():
        telemetry.record(SearchObservation("private", "cli", "", 42, []))
    result = RecallDiagnostics.recent_activity(settings)
    assert result.passed
    assert result.message == "1 queries, 100.0% empty, p50=42.0ms"


def test_read_failure_is_advisory() -> None:
    with patch(
        "quarry.doctor_recall.RecallInsights.read",
        side_effect=sqlite3.OperationalError("unreadable"),
    ):
        report = RecallDiagnostics.recent_activity(Settings())
    assert not report.passed and not report.required
    assert report.message == "local telemetry unavailable"
