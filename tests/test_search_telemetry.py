"""Exercise the installed telemetry facade at Quarry's search boundary."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from quarry.config import Settings
from quarry.recall_insights import RecallInsights
from quarry.recall_telemetry import RecallTelemetry, SearchObservation
from quarry.results import SearchResult
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


@pytest.fixture(autouse=True)
def observe_home(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """Give each test its own SQLite export and identifier salt."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("PUNT_TELEMETRY", raising=False)
    monkeypatch.delenv("QUARRY_TELEMETRY", raising=False)
    return tmp_path / ".punt-labs" / "quarry"


def test_search_exports_only_typed_metadata(tmp_path: Path, observe_home: Path) -> None:
    """Keep response content out of both span and event attributes."""
    daemon = InProcessDaemon(tmp_path / "daemon")
    hit = SearchResult.from_row(
        {
            "document_name": "private-document.pdf",
            "text": "private response content",
            "collection": "private-collection",
            "ingestion_timestamp": datetime.now(UTC).isoformat(),
        }
    )
    with (
        daemon.ctx.recall.running(),
        TestClient(daemon.app) as client,
        patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[hit]),
    ):
        scope = daemon.ctx.recall.database_hash()
        response = client.get(
            "/v1/search",
            params={
                "q": "private query",
                "surface": "arbitrary string",
                "agent_handle": "private-agent",
                "document": "private-filter",
            },
        )
        assert response.status_code == 200
        assert response.json()["results"][0]["text"] == "private response content"

    path = observe_home / "telemetry.db"
    with closing(sqlite3.connect(path)) as conn:
        dump = "\n".join(conn.iterdump())
    assert "private" not in dump
    assert '"surface":"unknown"' in dump.replace(" ", "")
    assert '"query_length":13' in dump.replace(" ", "")
    assert path.stat().st_mode & 0o777 == 0o600

    report = RecallInsights.read(scope)
    assert report.total_queries == report.memory_queries == 1
    assert report.per_agent_recall[0]["agent_handle"] != "private-agent"
    assert report.per_collection_hits[0]["hit_count"] == 1
    assert report.hit_decay_bands == [{"band": "0-7d", "hit_count": 1}]
    assert RecallInsights.read("other-scope").total_queries == 0


@pytest.mark.parametrize(
    "off_switch", ["settings", "PUNT_TELEMETRY", "QUARRY_TELEMETRY"]
)
def test_off_switch_opens_no_store(
    off_switch: str, observe_home: Path, monkeypatch: MonkeyPatch
) -> None:
    """An off switch needs neither the telemetry store nor the salt."""
    if off_switch != "settings":
        monkeypatch.setenv(off_switch, "off")
    telemetry = RecallTelemetry(Settings(telemetry_enabled=off_switch != "settings"))
    with telemetry.running():
        assert not telemetry.enabled
        telemetry.record(SearchObservation("unrecorded", "cli", "", 10, []))
    assert not observe_home.exists()


def test_unavailable_export_does_not_break_search(
    tmp_path: Path, observe_home: Path
) -> None:
    """A blocked export directory must not surface as an HTTP failure."""
    observe_home.parent.mkdir()
    observe_home.touch()
    daemon = InProcessDaemon(tmp_path / "daemon")
    with (
        daemon.ctx.recall.running(),
        TestClient(daemon.app) as client,
        patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]),
    ):
        response = client.get("/v1/search", params={"q": "test"})
        assert response.status_code == 200
        assert response.json()["results"] == []


def test_capture_failure_cannot_escape_after_a_search(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing capture produces a safe diagnostic and leaves the search intact."""
    daemon = InProcessDaemon(tmp_path / "daemon")
    with (
        daemon.ctx.recall.running(),
        TestClient(daemon.app) as client,
        patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]),
        patch.object(
            RecallTelemetry, "_record", side_effect=RuntimeError("private query")
        ),
    ):
        response = client.get("/v1/search", params={"q": "private query"})

    assert response.status_code == 200
    assert "Recall telemetry capture failed" in caplog.text
    assert "private query" not in caplog.text


def test_named_database_path_alias_keeps_recall_scope(tmp_path: Path) -> None:
    """Stopping and reopening via a symlink must keep the same history."""
    database = tmp_path / "database"
    database.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(database, target_is_directory=True)
    original = RecallTelemetry(Settings(lancedb_path=database))
    with original.running():
        original.record(SearchObservation("search", "cli", "", 10, []))

    reopened = RecallTelemetry(Settings(lancedb_path=alias))
    with reopened.running():
        assert RecallInsights.for_daemon(reopened).total_queries == 1


def test_environment_alone_cannot_enable_network_export(
    observe_home: Path, monkeypatch: MonkeyPatch
) -> None:
    """An exporter environment override cannot bypass explicit consent."""
    monkeypatch.setenv("PUNT_TELEMETRY_EXPORTER", "otlp")
    monkeypatch.setenv("PUNT_TELEMETRY_OTLP_ENDPOINT", "http://127.0.0.1:9")
    telemetry = RecallTelemetry(Settings())
    with telemetry.running():
        telemetry.record(SearchObservation("not exported", "cli", "", 10, []))
    assert (observe_home / "telemetry.db").exists()
    assert RecallInsights.read().total_queries == 1
