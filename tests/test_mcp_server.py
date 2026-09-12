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
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Self, final
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from starlette.testclient import TestClient

from quarry.client import QuarryClient, QuarryConnectionError
from quarry.client.transport import HttpxTransport, Response
from quarry.daemon.app import build_app
from quarry.daemon.context import DaemonContext
from quarry.db_pointer import SELECTION
from quarry.mcp_server import McpTools, mcp
from quarry.results import SearchResult

# The twelve tools the MCP surface exposes; a rename or removal is a regression.
_EXPECTED_TOOLS = {
    "find",
    "ingest",
    "remember",
    "learn",
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
class _TestClientTransport:
    """A :class:`QuarryClient` transport over Starlette's ``TestClient``.

    ``TestClient.request()`` (unlike a real ``httpx.Client``) warns on ANY
    ``timeout=`` argument — its in-memory ASGI transport has no socket to bound,
    so there is no non-deprecated way to pass one.  The production
    :class:`HttpxTransport` always forwards its caller's timeout, which is
    correct against a real server but trips that warning on every call here; this
    test-only transport reuses ``HttpxTransport``'s response parsing but never
    forwards a timeout to the request itself.
    """

    __slots__ = ("_client",)

    _client: TestClient

    def __new__(cls, client: TestClient) -> Self:
        self = super().__new__(cls)
        self._client = client
        return self

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        # Accepted for Transport-protocol parity; never forwarded (see class doc).
        timeout: float | None = None,
    ) -> Response:
        resp = self._client.request(
            method,
            path,
            params=dict(params) if params else None,
            json=dict(json_body) if json_body is not None else None,
        )
        return HttpxTransport._parse(resp)


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
        quarry_client = QuarryClient(_TestClientTransport(tc))
        self._tools = McpTools(connect=lambda: quarry_client)
        return self

    @property
    def tools(self) -> McpTools:
        return self._tools

    @property
    def http(self) -> TestClient:
        return self._client


# Bound on how long fixture teardown waits for a background job to finish for
# real before giving up and force-cancelling it (see the ``harness`` fixture).
_TEARDOWN_DRAIN_TIMEOUT_S = 10.0


async def _aclose_ingest_queue(ctx: DaemonContext) -> None:
    """Drain the ingest queue with the teardown timeout (keyword-only ``aclose``)."""
    await ctx.ingest_queue.aclose(drain_timeout=_TEARDOWN_DRAIN_TIMEOUT_S)


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[_ToolHarness]:
    """Yield a tool harness over a real daemon app, draining tasks on teardown.

    Teardown DRAINS (awaits real completion of) any still-running background
    job rather than only cancelling it: cancelling the wrapping ``asyncio.Task``
    does not stop a ``run_in_threadpool`` job's underlying OS thread, so a
    cancel-only teardown lets that thread keep running the (by-then unmocked)
    real ingest into a later test's mocks or a closed log stream. Only a
    straggler that outlives the drain window is force-cancelled.
    """
    ctx = DaemonContext(_mock_settings(tmp_path))
    _inject_mocks(ctx)
    with TestClient(build_app(ctx), raise_server_exceptions=False) as tc:
        yield _ToolHarness(tc)
        portal = tc.portal
        if portal is not None:
            # ``finally`` is load-bearing: a registry drain timeout now raises
            # (fail-closed) rather than swallowing, so the queue close must not
            # be skipped -- that queue is the OTHER leak vector this teardown
            # guards, tracked independently of ``TaskRegistry._refs``.
            try:
                portal.call(ctx.tasks.drain, _TEARDOWN_DRAIN_TIMEOUT_S)
            finally:
                portal.call(_aclose_ingest_queue, ctx)


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
        result = harness.tools.remember("reach me at jdoe@example.com", "note.md")
        assert "note.md" in result
        assert "task" in result

    def test_default_collection_is_empty_on_wire(self, harness: _ToolHarness) -> None:
        """MCP ``remember`` without ``collection`` sends the empty sentinel.

        The daemon chokepoint owns the routing rule (bug class 3): the surfaces
        must send ``collection=""`` when the caller does not name one, so the
        server-side rule is the single source of truth.
        """
        captured: list[str] = []
        real_route = __import__(
            "quarry.daemon.routes.ingestion", fromlist=["IngestionRoutes"]
        ).IngestionRoutes._remember_job

        def spy(self: object, body: dict[str, object]) -> object:
            captured.append(str(body.get("collection", "<missing>")))
            return real_route(self, body)

        with patch("quarry.daemon.routes.ingestion.IngestionRoutes._remember_job", spy):
            harness.tools.remember("body", "note.md", agent_handle="rmh")

        assert captured == [""]

    def test_agent_handle_routes_to_memory_collection(
        self, harness: _ToolHarness
    ) -> None:
        """End-to-end: MCP remember with an agent_handle lands in ``memory-<h>``.

        Bug-class-3 equivalence check: the CLI/MCP/HTTP surfaces must all reach
        the same server-side rule, so the observable outcome (the job's target
        collection) must be identical to the CLI path.
        """
        captured: list[str] = []
        real_route = __import__(
            "quarry.daemon.routes.ingestion", fromlist=["IngestionRoutes"]
        ).IngestionRoutes._remember_job

        def spy(self: object, body: dict[str, object]) -> object:
            job = real_route(self, body)
            captured.append(getattr(job, "collection", "<not-a-job>"))
            return job

        with patch("quarry.daemon.routes.ingestion.IngestionRoutes._remember_job", spy):
            harness.tools.remember("body", "note.md", agent_handle="rmh")

        assert captured == ["memory-rmh"]


class TestIngest:
    def test_non_url_points_to_register(self, harness: _ToolHarness) -> None:
        result = harness.tools.ingest("/local/file.pdf")
        assert "not a URL" in result
        assert "register" in result

    def test_url_dispatches(self, harness: _ToolHarness) -> None:
        # Mock ingest_auto so the async worker never runs a REAL fetch of the
        # example.com URL when it drains the queued job (the reject_reason patch
        # only gates the route, not the worker's later pipeline call).
        with (
            patch(
                "quarry.daemon.routes.ingestion.UrlSafetyCheck.reject_reason",
                return_value=None,
            ),
            patch(
                "quarry.ingestion.sitemap_ingest.ingest_auto",
                return_value={"document_name": "x", "collection": "c", "chunks": 1},
            ),
        ):
            result = harness.tools.ingest("https://example.com/doc")
            harness.http.get("/v1/tasks")  # let the worker drain the job under the mock
        assert "task" in result


class TestLearn:
    def test_dispatches_and_returns_task(self, harness: _ToolHarness) -> None:
        result = harness.tools.learn("always run make check before committing")
        assert "task" in result

    def test_blank_lesson_rejected(self, harness: _ToolHarness) -> None:
        result = harness.tools.learn("")
        assert result.startswith("Error:")
        assert "lesson" in result

    def test_topic_and_name_reach_the_daemon(self, harness: _ToolHarness) -> None:
        captured: list[dict[str, object]] = []
        real_learn_job = __import__(
            "quarry.daemon.routes.ingestion", fromlist=["IngestionRoutes"]
        ).IngestionRoutes._learn_job

        async def spy(self: object, body: dict[str, object]) -> object:
            captured.append(dict(body))
            return await real_learn_job(self, body)

        with patch("quarry.daemon.routes.ingestion.IngestionRoutes._learn_job", spy):
            harness.tools.learn("a lesson", topic="testing", name="auth-gotcha")

        assert len(captured) == 1
        assert captured[0]["lesson"] == "a lesson"
        assert captured[0]["topic"] == "testing"
        assert captured[0]["name"] == "auth-gotcha"


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

        original = SELECTION.active()
        try:
            result = harness.tools.use_database("coding")
            assert "coding" in result
            assert SELECTION.active() == "coding"
        finally:
            SELECTION.override(original or "")

    def test_remote_target_refuses_switch(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under a remote target, use() must NOT report a false local switch.

        A local db selection is ignored by TargetResolver when a remote target is
        active, so switching would silently leave subsequent tools on the remote
        daemon — data confusion. The tool returns an honest no-effect message and
        leaves the active db unchanged.
        """

        monkeypatch.setattr(
            "quarry.mcp_server.TargetResolver.selects_local_db",
            classmethod(lambda _cls: False),
        )
        original = SELECTION.active()
        try:
            SELECTION.override("start")
            result = harness.tools.use_database("coding")
            assert result.startswith("Error:")
            assert "remote" in result
            assert SELECTION.active() == "start", "must not switch under remote"
        finally:
            SELECTION.override(original or "")

    def test_quarry_url_env_refuses_switch_via_real_precedence(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Detection uses the real TargetResolver precedence: QUARRY_URL wins.

        No mock of the predicate — a set QUARRY_URL is tier 1, so selects_local_db
        is False and use() refuses, proving the guard matches what a real find/
        remember call would resolve.
        """

        monkeypatch.setenv("QUARRY_URL", "wss://remote.example.com:8420")
        original = SELECTION.active()
        try:
            SELECTION.override("start")
            result = harness.tools.use_database("coding")
            assert result.startswith("Error:")
            assert SELECTION.active() == "start"
        finally:
            SELECTION.override(original or "")

    def test_loopback_login_still_switches(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The post-install case: a `quarry login localhost` loopback login is
        LOCAL, so use() switches normally — not the remote-refusal regression.
        """

        monkeypatch.delenv("QUARRY_URL", raising=False)
        login = {"quarry": {"url": "wss://127.0.0.1:8420"}}
        monkeypatch.setattr("quarry.client.resolver.read_proxy_config", lambda: login)
        original = SELECTION.active()
        try:
            SELECTION.override("start")
            result = harness.tools.use_database("coding")
            assert not result.startswith("Error:"), result
            assert "coding" in result
            assert SELECTION.active() == "coding"
        finally:
            SELECTION.override(original or "")

    def test_default_selects_literal_default_not_persistent(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """use("default") targets the literal default db even when the persistent
        default is set to something else — the active selection and the summary agree.
        """
        from quarry.config import Settings

        # Persistent default is "coding"; use("default") must NOT pick it up.
        SELECTION.persist("coding")  # a real pointer file under the session home
        original = SELECTION.active()
        try:
            SELECTION.override("")  # nothing selected this session yet
            result = harness.tools.use_database("default")
            assert SELECTION.active() == "default"
            default_path = str(Settings.load().resolve_db_paths("default").lancedb_path)
            coding_path = str(Settings.load().resolve_db_paths("coding").lancedb_path)
            assert default_path in result
            assert coding_path not in result
        finally:
            SELECTION.override(original or "")

    def test_named_switch_sets_active_db(
        self, harness: _ToolHarness, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        SELECTION.persist("coding")  # a real pointer file under the session home
        original = SELECTION.active()
        try:
            harness.tools.use_database("coding")
            assert SELECTION.active() == "coding"
        finally:
            SELECTION.override(original or "")

    def test_round_trip_target_follows_selection(self, harness: _ToolHarness) -> None:
        """After use("work"), the active db resolves to work's target path."""
        from quarry.config import Settings

        original = SELECTION.active()
        try:
            result = harness.tools.use_database("work")
            work_path = str(Settings.load().resolve_db_paths("work").lancedb_path)
            assert SELECTION.active() == "work"
            assert work_path in result
        finally:
            SELECTION.override(original or "")

    def test_invalid_name_does_not_corrupt_state(self, harness: _ToolHarness) -> None:

        original = SELECTION.active()
        try:
            SELECTION.override("good")
            result = harness.tools.use_database("../evil")
            assert result.startswith("Error:")
            assert SELECTION.active() == "good"
        finally:
            SELECTION.override(original or "")


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

    def test_learn_returns_error_string(self) -> None:
        result = self._down_tools().learn("x")
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

        original = SELECTION.active()
        try:
            SELECTION.override("start")
            for name in ("", "   "):
                result = self._tools().use_database(name)
                assert result.startswith("Error:"), name
                assert SELECTION.active() == "start", "must not switch on blank"
        finally:
            SELECTION.override(original or "")

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


class TestToolDocstringOpeners:
    """Every tool docstring opens with an occasion, not a mechanism verb.

    The two trigger-carrying tools (find, remember) splice R1/R2/R3 verbatim;
    the rest lead with a situational "Use to ..." / "Use when ..." sentence
    per the design's per-tool table.
    """

    _R1 = (
        "Use find before WebSearch or WebFetch for research, or before "
        "answering a why/how/what-did-we-decide question."
    )
    _R2 = "Prefer grep for symbol and value lookups; prefer find for meaning."
    _R3 = (
        "Use remember when you learn something durable — a decision, a gotcha, "
        "a non-obvious fact, a procedure — so it survives context compaction."
    )
    _BOUNDARY = (
        "remember = a specific durable fact, ingest = a URL, learn = a "
        "distilled lesson that gets retrieval preference."
    )

    def test_server_instructions_lead_with_r1_and_r2(self) -> None:
        assert mcp.instructions is not None
        assert self._R1 in mcp.instructions
        assert self._R2 in mcp.instructions

    def test_find_docstring_carries_r1_and_r2(self) -> None:
        doc = McpTools.find.__doc__
        assert doc is not None
        assert self._R1 in doc
        assert self._R2 in doc

    def test_remember_docstring_carries_r3(self) -> None:
        doc = McpTools.remember.__doc__
        assert doc is not None
        assert self._R3 in doc

    def test_remember_drops_clipboard_framing(self) -> None:
        """R3a: the clipboard/API-response framing is dropped entirely."""
        doc = McpTools.remember.__doc__ or ""
        assert "clipboard" not in doc.lower()
        assert "api response" not in doc.lower()
        assert "sandbox-uploaded" not in doc.lower()

    def test_boundary_sentence_in_remember_ingest_learn_docstrings(self) -> None:
        """The three capture verbs must each carry the identical boundary
        sentence, not just the newcomer -- otherwise the boundary blurs for
        whichever verb is missing it.
        """
        for tool in (McpTools.remember, McpTools.ingest, McpTools.learn):
            doc = tool.__doc__ or ""
            assert self._BOUNDARY in doc, tool.__name__

    def test_other_tools_open_with_non_mechanism_sentence(self) -> None:
        """Every non-trigger tool opens with 'Use ...' — an occasion, not a verb.

        The nine remaining tools each get a situational opener per the design
        table. A mechanism opener would name the underlying operation (fetch,
        list, show, delete, register, ...); the required pattern instead names
        the occasion an agent would reach for it.
        """
        expected_openers = {
            "ingest": "Use when you have a URL to add to the knowledge base",
            "list_resources": "Use to see what's already indexed before ingesting",
            "show": "Use to read a specific page, or to check whether a document",
            "delete": "Use to remove stale or wrong content before re-ingesting",
            "register_directory": (
                "Use to track a local directory so future changes sync"
            ),
            "deregister_directory": "Use to stop tracking a directory",
            "sync_all_registrations": "Use after registering a new directory",
            "status": "Use to check how much is indexed",
            "use_database": "Use to point every other tool at a different",
        }
        for name, opener in expected_openers.items():
            doc = getattr(McpTools, name).__doc__
            assert doc is not None, name
            first_line = doc.lstrip().split("\n", 1)[0]
            assert opener in first_line, f"{name}: got {first_line!r}"
