"""Tests for the `GET /insights` route: aggregation, empty store, parity.

Uses :class:`tests.inproc_daemon.InProcessDaemon` (real handlers, tmp LanceDB,
no ONNX) so the "remote" path is the genuine daemon app, not a mock.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.config import Settings
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.daemon.routes.insights_builder import InsightsBuilder
from quarry.models import Chunk
from quarry.query_log import QueryLog, get_query_log
from quarry.query_log_types import QueryEvent, QueryHit
from tests.fakes import FakeEmbeddingBackend
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


def _hit(*, document_name: str = "report.pdf") -> QueryHit:
    return QueryHit(
        rank=1,
        document_name=document_name,
        collection="default",
        chunk_index=0,
        score=0.9,
        hit_agent_handle="",
        memory_type="",
    )


def _chunk(*, document_name: str, ingestion_timestamp: datetime) -> Chunk:
    return Chunk(
        document_name=document_name,
        document_path=f".tmp/{document_name}",
        collection="default",
        page_number=1,
        total_pages=1,
        chunk_index=0,
        text="chunk text",
        page_raw_text="raw text",
        page_type="text",
        source_format=".pdf",
        ingestion_timestamp=ingestion_timestamp,
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


class TestDisabledTelemetry:
    def test_insights_while_disabled_creates_no_file(self, tmp_path: Path) -> None:
        """A disabled toggle must never open (and so create) the store file."""
        get_query_log.cache_clear()
        (tmp_path / "lancedb").mkdir(parents=True, exist_ok=True)
        settings = Settings(
            lancedb_path=tmp_path / "lancedb",
            registry_path=tmp_path / "registry.db",
            telemetry_path=tmp_path / "telemetry.db",
            telemetry_enabled=False,
        )
        ctx = DaemonContext(
            settings, embedder=FakeEmbeddingBackend(settings.embedding_dimension)
        )
        with TestClient(build_app(ctx), raise_server_exceptions=False) as client:
            resp = client.get("/v1/insights")
        get_query_log.cache_clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["telemetry_enabled"] is False
        assert body["total_queries"] == 0
        assert not (tmp_path / "telemetry.db").exists()


class TestDecayBandBoundaries:
    """Direct, non-flaky boundary checks on ``InsightsBuilder._band`` (no I/O,
    no wall-clock dependency -- both endpoints and the day either side)."""

    @pytest.mark.parametrize(
        ("age_days", "expected_band"),
        [
            (0, "0-7d"),
            (7, "0-7d"),
            (8, "7-30d"),
            (30, "7-30d"),
            (31, "30-90d"),
            (90, "30-90d"),
            (91, "90d+"),
        ],
    )
    def test_band_for_age(self, age_days: int, expected_band: str) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        ingested_at = now - timedelta(days=age_days)
        assert InsightsBuilder._band(ingested_at, now) == expected_band


class TestHitDecayBandsEndToEnd:
    def test_hit_with_known_ingestion_lands_in_expected_band(
        self, tmp_path: Path
    ) -> None:
        get_query_log.cache_clear()
        daemon = InProcessDaemon(tmp_path)
        ingested_at = datetime.now(UTC) - timedelta(days=3)
        chunk = _chunk(document_name="doc.pdf", ingestion_timestamp=ingested_at)
        daemon.ctx.database.store.insert([chunk], np.zeros((1, 768), dtype=np.float32))

        log = QueryLog(daemon.ctx.settings.telemetry_path)
        log.record(_event(result_count=1), [_hit(document_name="doc.pdf")])
        log.close()

        with daemon.client() as client:
            resp = client.insights()
        get_query_log.cache_clear()

        bands = {row["band"]: row["hit_count"] for row in resp.hit_decay_bands}
        assert bands == {"0-7d": 1}

    def test_hit_absent_from_catalog_is_unknown(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        daemon = InProcessDaemon(tmp_path)
        log = QueryLog(daemon.ctx.settings.telemetry_path)
        log.record(_event(result_count=1), [_hit(document_name="ghost.pdf")])
        log.close()

        with daemon.client() as client:
            resp = client.insights()
        get_query_log.cache_clear()

        bands = {row["band"]: row["hit_count"] for row in resp.hit_decay_bands}
        assert bands == {"unknown": 1}

    def test_malformed_ingestion_timestamp_is_skipped_and_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        get_query_log.cache_clear()
        daemon = InProcessDaemon(tmp_path)
        # ChunkCatalog is __slots__-only (no instance __dict__), so the class
        # attribute is the patch point, not the instance.
        monkeypatch.setattr(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
            lambda self, *_args, **_kwargs: [
                {
                    "document_name": "doc.pdf",
                    "collection": "default",
                    "ingestion_timestamp": "not-a-timestamp",
                }
            ],
        )
        log = QueryLog(daemon.ctx.settings.telemetry_path)
        log.record(_event(result_count=1), [_hit(document_name="doc.pdf")])
        log.close()

        with caplog.at_level(logging.WARNING), daemon.client() as client:
            resp = client.insights()
        get_query_log.cache_clear()

        bands = {row["band"]: row["hit_count"] for row in resp.hit_decay_bands}
        assert bands == {"unknown": 1}
        assert any(
            "malformed ingestion_timestamp" in record.message
            for record in caplog.records
        )
