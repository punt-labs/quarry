"""Tests for the recall-telemetry write point at ``GET /search``.

Builds its own minimal daemon fixture (rather than reusing
``test_http_server.py``'s) so ``telemetry_enabled`` defaults to ``True`` here
and every test's intent is visible without cross-referencing another file's
shared fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.query_log import QueryLog, get_query_log
from quarry.results import SearchResult

_FAKE_GH_PAT = "ghp_" + "a" * 36


def _mock_settings(tmp_path: Path, *, telemetry_enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
    s.registry_path = tmp_path / "registry.db"
    s.telemetry_path = tmp_path / "telemetry.db"
    s.telemetry_enabled = telemetry_enabled
    s.telemetry_retention_days = 90
    s.telemetry_prune_cadence_s = 3600.0
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    s.ingest_queue_depth = 32
    s.ingest_embed_concurrency = 1
    s.ingest_drain_timeout_s = 30.0
    s.ingest_max_workers = 256
    s.ingest_worker_idle_s = 60.0
    s.watch_enabled = False
    return s


def _inject_mocks(ctx: DaemonContext) -> None:
    embedder = MagicMock()
    embedder.embed_query.return_value = np.zeros(768, dtype=np.float32)
    embedder.dimension = 768
    embedder.model_name = "test-model"
    ctx._resources.__dict__["embedder"] = embedder


def _client(tmp_path: Path, *, telemetry_enabled: bool = True) -> TestClient:
    settings = _mock_settings(tmp_path, telemetry_enabled=telemetry_enabled)
    ctx = DaemonContext(settings)
    _inject_mocks(ctx)
    return TestClient(build_app(ctx), raise_server_exceptions=False)


_ONE_RESULT: list[dict[str, Any]] = [
    {
        "document_name": "report.pdf",
        "collection": "default",
        "page_number": 1,
        "chunk_index": 0,
        "text": "hello world",
        "page_type": "text",
        "source_format": ".pdf",
        "agent_handle": "",
        "memory_type": "",
        "_distance": 0.1,
    }
]


def _patched_retrieve(rows: list[dict[str, Any]]) -> Any:
    return patch(
        "quarry.retrieval.hybrid.HybridRetriever.retrieve",
        return_value=[SearchResult.from_row(r) for r in rows],
    )


class TestWriteOnSearch:
    def test_search_writes_one_event_and_its_hits(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        client = _client(tmp_path)
        with _patched_retrieve(_ONE_RESULT):
            resp = client.get("/v1/search?q=hello&surface=cli")
        assert resp.status_code == 200

        log = QueryLog(tmp_path / "telemetry.db")
        agg = log.insights.aggregate()
        hits = log.insights.recent_hits()
        log.close()
        get_query_log.cache_clear()

        assert agg.total_queries == 1
        assert hits == [{"document_name": "report.pdf", "collection": "default"}]

    def test_toggle_off_writes_nothing(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        client = _client(tmp_path, telemetry_enabled=False)
        with _patched_retrieve(_ONE_RESULT):
            resp = client.get("/v1/search?q=hello")
        assert resp.status_code == 200

        assert not (tmp_path / "telemetry.db").exists()

    def test_recorded_surface_defaults_when_absent(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        client = _client(tmp_path)
        with _patched_retrieve([]):
            client.get("/v1/search?q=hello")  # no ?surface=

        log = QueryLog(tmp_path / "telemetry.db")
        row = log._conn.execute("SELECT surface FROM query_events").fetchone()
        log.close()
        get_query_log.cache_clear()
        assert row is not None
        assert row[0] == "unknown"


class TestScrubBeforePersist:
    def test_seeded_secret_never_appears_in_query_scrubbed(
        self, tmp_path: Path
    ) -> None:
        get_query_log.cache_clear()
        client = _client(tmp_path)
        query = f"find the token {_FAKE_GH_PAT} please"
        with _patched_retrieve([]):
            client.get(f"/v1/search?q={query}")

        log = QueryLog(tmp_path / "telemetry.db")
        row = log._conn.execute("SELECT query_scrubbed FROM query_events").fetchone()
        log.close()
        get_query_log.cache_clear()

        assert row is not None
        assert _FAKE_GH_PAT not in row[0]
        assert "[REDACTED:gh-pat]" in row[0]


class TestFilterReachesTelemetry:
    def test_search_filters_are_recorded_on_the_event(self, tmp_path: Path) -> None:
        """Bug class 3: a CLI/MCP filter must reach the recorded row, not just
        the search itself."""
        get_query_log.cache_clear()
        client = _client(tmp_path)
        with _patched_retrieve([]):
            client.get(
                "/v1/search?q=hello&collection=alpha&agent_handle=rmh&memory_type=fact"
            )

        log = QueryLog(tmp_path / "telemetry.db")
        row = log._conn.execute(
            "SELECT collection, agent_handle, filters_json FROM query_events"
        ).fetchone()
        log.close()
        get_query_log.cache_clear()

        assert row is not None
        collection, agent_handle, filters_json = row
        assert collection == "alpha"
        assert agent_handle == "rmh"
        filters = json.loads(filters_json)
        assert filters["collection"] == "alpha"
        assert filters["agent_handle"] == "rmh"
        assert filters["memory_type"] == "fact"


class TestSurfaceProvenance:
    @pytest.mark.parametrize("surface", ["cli", "mcp", "http", "plugin"])
    def test_known_surface_reaches_telemetry(
        self, tmp_path: Path, surface: str
    ) -> None:
        get_query_log.cache_clear()
        client = _client(tmp_path)
        with _patched_retrieve([]):
            client.get(f"/v1/search?q=hello&surface={surface}")

        log = QueryLog(tmp_path / "telemetry.db")
        row = log._conn.execute("SELECT surface FROM query_events").fetchone()
        log.close()
        get_query_log.cache_clear()

        assert row is not None
        assert row[0] == surface

    def test_unrecognized_surface_coerces_to_unknown(self, tmp_path: Path) -> None:
        get_query_log.cache_clear()
        client = _client(tmp_path)
        with _patched_retrieve([]):
            client.get("/v1/search?q=hello&surface=bogus")

        log = QueryLog(tmp_path / "telemetry.db")
        row = log._conn.execute("SELECT surface FROM query_events").fetchone()
        log.close()
        get_query_log.cache_clear()

        assert row is not None
        assert row[0] == "unknown"


class TestFailureInjection:
    def test_telemetry_write_failure_never_breaks_find(self, tmp_path: Path) -> None:
        """A raising ``QueryLog.record`` must not turn a search into a 500."""
        get_query_log.cache_clear()
        client = _client(tmp_path)
        with (
            _patched_retrieve(_ONE_RESULT),
            patch.object(QueryLog, "record", side_effect=RuntimeError("disk full")),
        ):
            resp = client.get("/v1/search?q=hello")
        get_query_log.cache_clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_results"] == 1
        assert body["results"][0]["document_name"] == "report.pdf"


@pytest.mark.resource
class TestNoDescriptorLeakAcrossSearches:
    def test_many_searches_do_not_leak_descriptors(self, tmp_path: Path) -> None:
        if not Path("/proc/self/fd").is_dir():
            pytest.skip("no /proc/self/fd on this platform")

        def _open_fd_count() -> int:
            return sum(1 for _ in Path("/proc/self/fd").iterdir())

        get_query_log.cache_clear()
        client = _client(tmp_path)
        with _patched_retrieve(_ONE_RESULT):
            before = _open_fd_count()
            for _ in range(200):
                client.get("/v1/search?q=hello")
            after = _open_fd_count()
        get_query_log.cache_clear()

        assert after - before <= 5, (
            f"fd count grew {before} -> {after} over 200 searches"
        )
