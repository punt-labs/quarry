"""Tests for the `GET /insights` route: aggregation, empty store, parity.

Uses :class:`tests.inproc_daemon.InProcessDaemon` (real handlers, tmp LanceDB,
no ONNX) so the "remote" path is the genuine daemon app, not a mock.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from quarry.query_log import QueryEvent, QueryHit, QueryLog, get_query_log
from tests.inproc_daemon import InProcessDaemon


def _event(
    *, agent_handle: str = "", result_count: int = 1, latency_ms: float = 10.0
) -> QueryEvent:
    return QueryEvent(
        ts=datetime.now(UTC).isoformat(),
        surface="cli",
        agent_handle=agent_handle,
        collection="default",
        filters_json="{}",
        limit_n=10,
        latency_ms=latency_ms,
        result_count=result_count,
        query_scrubbed="hello world",
        query_len=11,
    )


def _hit() -> QueryHit:
    return QueryHit(
        rank=1,
        document_name="report.pdf",
        collection="default",
        chunk_index=0,
        score=0.9,
        hit_agent_handle="",
        memory_type="",
    )


class TestEmptyStore:
    def test_empty_store_returns_zeroed_defaults(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        daemon = InProcessDaemon(tmp_path)
        with daemon.client() as client:
            resp = client.insights()
        get_query_log.cache_clear()

        assert resp.total_queries == 0
        assert resp.empty_result_rate == 0.0
        assert resp.p50_latency_ms == 0.0
        assert resp.p95_latency_ms == 0.0
        assert resp.top_empty_queries == []
        assert resp.per_collection_hits == []
        assert resp.memory_queries == 0
        assert resp.knowledge_queries == 0


class TestAggregationOnSeededEvents:
    def test_seeded_events_produce_the_expected_snapshot(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        daemon = InProcessDaemon(tmp_path)
        log = QueryLog(daemon.ctx.settings.telemetry_path)
        log.record(_event(agent_handle="rmh", result_count=1), [_hit()])
        log.record(_event(agent_handle="", result_count=0), [])
        log.close()

        with daemon.client() as client:
            resp = client.insights()
        get_query_log.cache_clear()

        assert resp.total_queries == 2
        assert resp.empty_result_rate == 0.5
        assert resp.memory_queries == 1
        assert resp.knowledge_queries == 1
        assert resp.per_collection_hits == [{"collection": "default", "hit_count": 1}]


class TestLocalRemoteParity:
    def test_client_model_and_raw_json_share_identical_field_names(
        self, tmp_path: Path
    ) -> None:
        """Bug class 3: the client model must be a faithful superset of nothing
        more, nothing less than what the daemon actually returns."""
        get_query_log.cache_clear()
        daemon = InProcessDaemon(tmp_path)
        log = QueryLog(daemon.ctx.settings.telemetry_path)
        log.record(_event(), [_hit()])
        log.close()

        raw: dict[str, Any] = (
            TestClient(daemon.app, raise_server_exceptions=False)
            .get("/v1/insights")
            .json()
        )
        with daemon.client() as client:
            resp = client.insights()
        get_query_log.cache_clear()

        assert set(raw) == set(resp.model_dump())

    def test_row_breakdown_field_names_match(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        daemon = InProcessDaemon(tmp_path)
        log = QueryLog(daemon.ctx.settings.telemetry_path)
        log.record(_event(agent_handle="rmh"), [_hit()])
        log.close()

        raw: dict[str, Any] = (
            TestClient(daemon.app, raise_server_exceptions=False)
            .get("/v1/insights")
            .json()
        )
        with daemon.client() as client:
            resp = client.insights()
        get_query_log.cache_clear()

        assert raw["per_collection_hits"] == resp.model_dump()["per_collection_hits"]
        assert raw["per_agent_recall"] == resp.model_dump()["per_agent_recall"]
        assert set(raw["per_collection_hits"][0]) == {"collection", "hit_count"}
        assert set(raw["per_agent_recall"][0]) == {"agent_handle", "query_count"}
