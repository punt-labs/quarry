"""Tests for ``quarry mcp`` — the client-tier FastMCP server (DES-031 v2.2).

Every tool is a thin :class:`~quarry.client.QuarryClient` call, so these tests
drive each :class:`~quarry.mcp_server.McpTools` method through a ``QuarryClient``
whose transport is an in-process ``httpx``/ASGI client over the **real** daemon
app (``build_app``) with a temp LanceDB and a stub embedder.  The fake runs the
real ``/v1`` handlers, so a tool response cannot drift from the wire contract
(bug-class-3 parity), and no engine is mocked at the MCP boundary.

A separate suite asserts the MCP surface is complete (no tool removed/renamed)
and that a down daemon surfaces as a clean MCP error string, never a raised
exception or an in-process engine fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Self, final
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.client import QuarryClient, QuarryConnectionError
from quarry.client.transport import HttpxTransport
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.mcp_server import McpTools, mcp
from quarry.results import SearchResult

# The eleven tools the MCP surface exposes; a rename or removal is a regression.
_EXPECTED_TOOLS = {
    "find",
    "ingest",
    "remember",
    "list",
    "show",
    "delete",
    "register_directory",
    "deregister_directory",
    "sync_all_registrations",
    "status",
    "use",
}


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.lancedb_path.mkdir(parents=True)
    s.registry_path = tmp_path / "registry.db"
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    s.ingest_queue_depth = 32
    s.ingest_embed_concurrency = 1
    s.ingest_drain_timeout_s = 30.0
    s.ingest_max_workers = 256
    s.ingest_worker_idle_s = 60.0
    return s


def _inject_mocks(ctx: DaemonContext) -> None:
    """Replace the daemon's ONNX embedder with a stub; keep the real DB layer."""
    embedder = MagicMock()
    embedder.embed_query.return_value = np.zeros(768, dtype=np.float32)
    embedder.dimension = 768
    embedder.model_name = "test-model"
    ctx._resources.__dict__["embedder"] = embedder


@final
class _ToolHarness:
    """Bind an :class:`McpTools` to a client over the real daemon app.

    Holds the ``TestClient`` so a test can also assert daemon-side state, and
    exposes the ``McpTools`` whose ``connect`` factory returns a ``QuarryClient``
    over that same app — the tool round-trips through the real ``/v1`` handlers.
    """

    __slots__ = ("_client", "_tools")

    _client: TestClient
    _tools: McpTools

    def __new__(cls, tc: TestClient) -> Self:
        self = super().__new__(cls)
        self._client = tc
        quarry_client = QuarryClient(HttpxTransport(tc))
        self._tools = McpTools(connect=lambda: quarry_client)
        return self

    @property
    def tools(self) -> McpTools:
        return self._tools

    @property
    def http(self) -> TestClient:
        return self._client


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[_ToolHarness]:
    """Yield a tool harness over a real daemon app, draining tasks on teardown."""
    ctx = DaemonContext(_mock_settings(tmp_path))
    _inject_mocks(ctx)
    with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
        yield _ToolHarness(tc)
        portal = tc.portal
        if portal is not None:
            portal.call(ctx.tasks.cancel_all)
            portal.call(ctx.ingest_queue.cancel_workers)


class TestSurfaceComplete:
    """The MCP surface must stay exactly the eleven documented tools."""

    def test_all_tools_registered(self) -> None:
        names = {tool.name for tool in asyncio.run(mcp.list_tools())}
        assert names == _EXPECTED_TOOLS

    def test_every_tool_has_a_description(self) -> None:
        for tool in asyncio.run(mcp.list_tools()):
            assert tool.description, f"{tool.name} has no description"


class TestFind:
    def test_returns_results(self, harness: _ToolHarness) -> None:
        rows = [
            {
                "document_name": "report.pdf",
                "collection": "finance",
                "page_number": 3,
                "chunk_index": 0,
                "text": "quarterly revenue grew",
                "page_type": "text",
                "source_format": ".pdf",
                "_distance": 0.15,
            }
        ]
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve",
            return_value=[SearchResult.from_row(r) for r in rows],
        ):
            result = harness.tools.find("revenue growth")
        assert "revenue growth" in result
        assert "1 result" in result
        assert "report.pdf" in result
        assert "quarterly revenue grew" in result

    def test_clamps_limit_to_50(self, harness: _ToolHarness) -> None:
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as retrieve:
            harness.tools.find("test", limit=100)
        assert retrieve.call_args.kwargs["limit"] == 50

    def test_filter_reaches_daemon_query(self, harness: _ToolHarness) -> None:
        """A CLI/MCP filter param must reach the daemon's DB query (bug class 3)."""
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as retrieve:
            harness.tools.find("test", collection="math", page_type="code")
        search_filter = retrieve.call_args.kwargs["search_filter"]
        assert search_filter.collection == "math"
        assert search_filter.page_type == "code"

    def test_empty_filter_passes_none(self, harness: _ToolHarness) -> None:
        with patch(
            "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
        ) as retrieve:
            harness.tools.find("test")
        assert retrieve.call_args.kwargs["search_filter"].collection is None

    def test_no_results_message(self, harness: _ToolHarness) -> None:
        with patch("quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]):
            result = harness.tools.find("nothing matches")
        assert "No results" in result


class TestStatus:
    def test_returns_status_fields(self, harness: _ToolHarness) -> None:
        cols = [{"collection": "math", "document_count": 2, "chunk_count": 42}]
        with (
            patch("quarry.db.chunk_store.ChunkStore.count", return_value=42),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections",
                return_value=cols,
            ),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
                return_value=[{"document_name": "a.pdf"}, {"document_name": "b.pdf"}],
            ),
        ):
            result = harness.tools.status()
        assert "Documents:" in result
        assert "Chunks:" in result
        assert "42" in result
        assert "snowflake-arctic-embed-m-v1.5" in result


class TestListResources:
    def test_documents(self, harness: _ToolHarness) -> None:
        docs = [
            {
                "document_name": "a.pdf",
                "document_path": "/d/a.pdf",
                "collection": "math",
                "total_pages": 10,
                "chunk_count": 25,
                "indexed_pages": 10,
                "ingestion_timestamp": "2026-01-01",
            }
        ]
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=docs
        ):
            result = harness.tools.list_resources("documents")
        assert "a.pdf" in result
        assert "DOCUMENT" in result

    def test_documents_filter_reaches_daemon(self, harness: _ToolHarness) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
        ) as list_docs:
            harness.tools.list_resources("documents", collection="math")
        assert list_docs.call_args.kwargs["collection_filter"] == "math"

    def test_collections(self, harness: _ToolHarness) -> None:
        cols = [{"collection": "math", "document_count": 5, "chunk_count": 100}]
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_collections", return_value=cols
        ):
            result = harness.tools.list_resources("collections")
        assert "math" in result
        assert "COLLECTION" in result

    def test_databases(self, harness: _ToolHarness) -> None:
        result = harness.tools.list_resources("databases")
        # The daemon reports the single database it is fixed to.
        assert "DATABASE" in result

    def test_registrations_empty(self, harness: _ToolHarness) -> None:
        result = harness.tools.list_resources("registrations")
        assert "No registered directories" in result

    def test_unknown_kind(self, harness: _ToolHarness) -> None:
        result = harness.tools.list_resources("bogus")
        assert "unknown kind" in result


class TestShow:
    def test_page_text(self, harness: _ToolHarness) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.get_page_text",
            return_value="The quick brown fox",
        ):
            result = harness.tools.show("report.pdf", page_number=3)
        assert "Page: 3" in result
        assert "The quick brown fox" in result

    def test_metadata(self, harness: _ToolHarness) -> None:
        doc = {
            "document_name": "report.pdf",
            "document_path": "/d/report.pdf",
            "collection": "math",
            "total_pages": 10,
            "chunk_count": 42,
            "indexed_pages": 10,
            "ingestion_timestamp": "2026-01-01",
        }
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[doc]
        ):
            result = harness.tools.show("report.pdf")
        assert "report.pdf" in result
        assert "math" in result

    def test_missing_page_is_friendly_not_found(self, harness: _ToolHarness) -> None:
        """A 404 renders the plain domain message, not "Error: HttpError"."""
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.get_page_text", return_value=None
        ):
            result = harness.tools.show("missing.pdf", page_number=99)
        assert result == "No data found for missing.pdf page 99"

    def test_missing_document_is_friendly_not_found(
        self, harness: _ToolHarness
    ) -> None:
        with patch(
            "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
        ):
            result = harness.tools.show("missing.pdf")
        assert result == "Document 'missing.pdf' not found"


class TestRemember:
    def test_dispatches_and_returns_task(self, harness: _ToolHarness) -> None:
        result = harness.tools.remember("# Hello\nWorld", "notes.md")
        assert "notes.md" in result
        assert "task" in result

    def test_scrubs_via_daemon(self, harness: _ToolHarness) -> None:
        """The daemon owns scrubbing; a 202 comes back before any indexing."""
        result = harness.tools.remember("reach me at jmf@pobox.com", "note.md")
        assert "note.md" in result
        assert "task" in result


class TestIngest:
    def test_non_url_points_to_register(self, harness: _ToolHarness) -> None:
        result = harness.tools.ingest("/local/file.pdf")
        assert "not a URL" in result
        assert "register" in result

    def test_url_dispatches(self, harness: _ToolHarness) -> None:
        with patch(
            "quarry.daemon.routes.ingestion.UrlSafetyCheck.reject_reason",
            return_value=None,
        ):
            result = harness.tools.ingest("https://example.com/doc")
        assert "task" in result


class TestDelete:
    def test_document_dispatches(self, harness: _ToolHarness) -> None:
        result = harness.tools.delete("report.pdf")
        assert "report.pdf" in result
        assert "task" in result

    def test_collection_dispatches(self, harness: _ToolHarness) -> None:
        result = harness.tools.delete("math", kind="collection")
        assert "math" in result
        assert "task" in result

    def test_invalid_kind(self, harness: _ToolHarness) -> None:
        result = harness.tools.delete("x", kind="bogus")
        assert "Invalid kind" in result


class TestRegisterDeregister:
    def test_register_dispatches(self, harness: _ToolHarness, tmp_path: Path) -> None:
        target = tmp_path / "course"
        target.mkdir()
        with patch(
            "quarry.daemon.routes.registrations.RegistrationRoutes._server_home",
            return_value=(tmp_path, None),
        ):
            result = harness.tools.register_directory(str(target), "my-course")
        assert "my-course" in result
        assert "task" in result

    def test_register_outside_home_is_clean_error(
        self, harness: _ToolHarness, tmp_path: Path
    ) -> None:
        target = tmp_path / "outside"
        target.mkdir()
        with patch(
            "quarry.daemon.routes.registrations.RegistrationRoutes._server_home",
            return_value=(tmp_path / "elsewhere", None),
        ):
            result = harness.tools.register_directory(str(target))
        assert result.startswith("Error:")

    def test_deregister_unknown_is_clean_error(self, harness: _ToolHarness) -> None:
        result = harness.tools.deregister_directory("ghost")
        assert result.startswith("Error:")


class TestSync:
    def test_dispatches(self, harness: _ToolHarness) -> None:
        result = harness.tools.sync_all_registrations()
        assert "task" in result


class TestUseDatabase:
    def test_switch(self, harness: _ToolHarness) -> None:
        from quarry.config import Settings

        original = Settings.active_db()
        try:
            result = harness.tools.use_database("coding")
            assert "coding" in result
            assert Settings.active_db() == "coding"
        finally:
            Settings.set_active_db(original or "")

    def test_remote_target_refuses_switch(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under a remote target, use() must NOT report a false local switch.

        A local db selection is ignored by TargetResolver when a remote target is
        active, so switching would silently leave subsequent tools on the remote
        daemon — data confusion. The tool returns an honest no-effect message and
        leaves the active db unchanged.
        """
        from quarry.config import Settings

        monkeypatch.setattr(
            "quarry.mcp_server.TargetResolver.selects_local_db",
            classmethod(lambda _cls: False),
        )
        original = Settings.active_db()
        try:
            Settings.set_active_db("start")
            result = harness.tools.use_database("coding")
            assert result.startswith("Error:")
            assert "remote" in result
            assert Settings.active_db() == "start", "must not switch under remote"
        finally:
            Settings.set_active_db(original or "")

    def test_quarry_url_env_refuses_switch_via_real_precedence(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Detection uses the real TargetResolver precedence: QUARRY_URL wins.

        No mock of the predicate — a set QUARRY_URL is tier 1, so selects_local_db
        is False and use() refuses, proving the guard matches what a real find/
        remember call would resolve.
        """
        from quarry.config import Settings

        monkeypatch.setenv("QUARRY_URL", "wss://remote.example.com:8420")
        original = Settings.active_db()
        try:
            Settings.set_active_db("start")
            result = harness.tools.use_database("coding")
            assert result.startswith("Error:")
            assert Settings.active_db() == "start"
        finally:
            Settings.set_active_db(original or "")

    def test_loopback_login_still_switches(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The post-install case: a `quarry login localhost` loopback login is
        LOCAL, so use() switches normally — not the remote-refusal regression.
        """
        from quarry.config import Settings

        monkeypatch.delenv("QUARRY_URL", raising=False)
        login = {"quarry": {"url": "wss://127.0.0.1:8420"}}
        monkeypatch.setattr("quarry.client.resolver.read_proxy_config", lambda: login)
        original = Settings.active_db()
        try:
            Settings.set_active_db("start")
            result = harness.tools.use_database("coding")
            assert not result.startswith("Error:"), result
            assert "coding" in result
            assert Settings.active_db() == "coding"
        finally:
            Settings.set_active_db(original or "")

    def test_default_selects_literal_default_not_persistent(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """use("default") targets the literal default db even when the persistent
        default is set to something else — active_db() and the summary path agree.
        """
        from quarry.config import Settings

        # Persistent default is "coding"; use("default") must NOT pick it up.
        monkeypatch.setattr(
            Settings, "read_default_db", classmethod(lambda _cls: "coding")
        )
        original = Settings.active_db()
        try:
            Settings.set_active_db("")  # nothing selected this session yet
            result = harness.tools.use_database("default")
            assert Settings.active_db() == "default"
            default_path = str(Settings.load().resolve_db_paths("default").lancedb_path)
            coding_path = str(Settings.load().resolve_db_paths("coding").lancedb_path)
            assert default_path in result
            assert coding_path not in result
        finally:
            Settings.set_active_db(original or "")

    def test_named_switch_sets_active_db(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from quarry.config import Settings

        monkeypatch.setattr(
            Settings, "read_default_db", classmethod(lambda _cls: "coding")
        )
        original = Settings.active_db()
        try:
            harness.tools.use_database("coding")
            assert Settings.active_db() == "coding"
        finally:
            Settings.set_active_db(original or "")

    def test_round_trip_target_follows_selection(self, harness: _ToolHarness) -> None:
        """After use("work"), the active db resolves to work's target path."""
        from quarry.config import Settings

        original = Settings.active_db()
        try:
            result = harness.tools.use_database("work")
            work_path = str(Settings.load().resolve_db_paths("work").lancedb_path)
            assert Settings.active_db() == "work"
            assert work_path in result
        finally:
            Settings.set_active_db(original or "")

    def test_invalid_name_does_not_corrupt_state(self, harness: _ToolHarness) -> None:
        from quarry.config import Settings

        original = Settings.active_db()
        try:
            Settings.set_active_db("good")
            result = harness.tools.use_database("../evil")
            assert result.startswith("Error:")
            assert Settings.active_db() == "good"
        finally:
            Settings.set_active_db(original or "")


class TestDaemonDown:
    """A down daemon surfaces as a clean MCP error, never an engine fallback."""

    @staticmethod
    def _down_tools() -> McpTools:
        def _connect() -> QuarryClient:
            raise QuarryConnectionError("quarryd is not running", "127.0.0.1")

        return McpTools(connect=_connect)

    def test_find_returns_error_string(self) -> None:
        result = self._down_tools().find("anything")
        assert result.startswith("Error:")
        assert "not running" in result

    def test_status_returns_error_string(self) -> None:
        result = self._down_tools().status()
        assert result.startswith("Error:")

    def test_remember_returns_error_string(self) -> None:
        result = self._down_tools().remember("x", "n.md")
        assert result.startswith("Error:")


class TestInputValidation:
    """Malformed inputs are rejected/normalized without hitting the daemon.

    Blank required args and non-positive numbers are caller errors — they short-
    circuit before ``_connect``, so a client that raises on connect proves the
    guard fired first. Valid inputs keep the exact behavior mdm verified.
    """

    @staticmethod
    def _tools() -> McpTools:
        def _connect() -> QuarryClient:
            raise AssertionError("guard must short-circuit before connecting")

        return McpTools(connect=_connect)

    def test_find_blank_query(self) -> None:
        result = self._tools().find("   ")
        assert result.startswith("Error:")
        assert "query" in result

    def test_find_non_positive_limit(self) -> None:
        result = self._tools().find("q", limit=0)
        assert result.startswith("Error:")
        assert "limit" in result

    def test_remember_blank_content(self) -> None:
        result = self._tools().remember("   ", "notes.md")
        assert result.startswith("Error:")
        assert "content" in result

    def test_remember_blank_document_name(self) -> None:
        result = self._tools().remember("body", "  ")
        assert result.startswith("Error:")
        assert "document_name" in result

    def test_delete_blank_name(self) -> None:
        result = self._tools().delete("")
        assert result.startswith("Error:")
        assert "name" in result

    def test_register_blank_directory(self) -> None:
        result = self._tools().register_directory("   ")
        assert result.startswith("Error:")
        assert "directory" in result

    def test_deregister_blank_collection(self) -> None:
        result = self._tools().deregister_directory("")
        assert result.startswith("Error:")
        assert "collection" in result

    def test_use_blank_name(self) -> None:
        from quarry.config import Settings

        original = Settings.active_db()
        try:
            Settings.set_active_db("start")
            for name in ("", "   "):
                result = self._tools().use_database(name)
                assert result.startswith("Error:"), name
                assert Settings.active_db() == "start", "must not switch on blank"
        finally:
            Settings.set_active_db(original or "")

    def test_show_negative_page_is_metadata_not_daemon_error(
        self, harness: _ToolHarness
    ) -> None:
        """page_number <= 0 means no page (metadata), never a daemon 400.

        With a missing document the metadata path yields the friendly not-found,
        proving -1 was NOT sent to the daemon as an invalid page.
        """
        for page in (-1, 0):
            with patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
                return_value=[],
            ):
                result = harness.tools.show("missing.pdf", page_number=page)
            assert result == "Document 'missing.pdf' not found", page
