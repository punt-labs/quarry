"""Exercise the HTTP and client views of the shared local recall export."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.recall_telemetry import SearchObservation
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch


def test_disabled_snapshot_does_not_open_store(tmp_path: Path) -> None:
    """Calls without a running provider have the standard disabled shape."""
    daemon = InProcessDaemon(tmp_path)
    with daemon.client() as client:
        report = client.insights()
    assert not report.telemetry_enabled
    assert report.total_queries == 0


def test_insights_aggregates_only_current_database(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Seed real exports and reconstruct insights through the HTTP client."""
    monkeypatch.setenv("HOME", str(tmp_path))
    alpha = InProcessDaemon(tmp_path / "alpha")
    beta = InProcessDaemon(tmp_path / "beta")
    with alpha.ctx.recall.running():
        alpha.ctx.recall.record(SearchObservation("empty", "mcp", "", 40, []))
        alpha.ctx.recall.record(SearchObservation("empty", "http", "my-agent", 20, []))
    with beta.ctx.recall.running():
        beta.ctx.recall.record(SearchObservation("other", "cli", "", 5, []))
    with alpha.ctx.recall.running(), alpha.client() as client:
        report = client.insights()
    assert report.telemetry_enabled
    assert report.total_queries == 2
    assert report.empty_result_rate == 1
    assert report.memory_queries == report.knowledge_queries == 1
    assert report.p50_latency_ms == 20
    assert report.p95_latency_ms == 40
    assert report.top_empty_queries[0]["count"] == 2
    assert report.top_empty_queries[0]["query_hash"] != "empty"
