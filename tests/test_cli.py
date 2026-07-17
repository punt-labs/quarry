"""CLI param-parity and field-emission tests (bug class 3), driven end-to-end.

Every data command is a pure ``QuarryClient`` call, so these tests inject a
recording transport (via ``TargetResolver.connect``) and assert that every
parameter the CLI encodes reaches the daemon request, and that the emitted JSON
carries the wire model's field names.  A recording transport — rather than mocks
— keeps the request/response shape exactly what the real transport would carry.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Self, final
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from quarry.__main__ import app
from quarry.client import QuarryClient, TargetResolver
from quarry.client.transport import Response

runner = CliRunner()

# A completed task body the poll loop terminates on; ``results`` carries the
# per-operation keys each awaiting command reads.
_TASK_RESULTS = {
    "deleted": 3,
    "deleted_chunks": 5,
    "removed": 2,
    "optimized": True,
    "fragments_before": 9,
    "ingested": 1,
    "skipped_existing": 0,
    "skipped_unregistered": 0,
    "skipped_empty": 0,
    "errors": [],
    "dry_run": False,
}

# Canned 2xx bodies keyed by (method, path-without-query). Each satisfies the
# quarry.api response model the client parses for that route.
_BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("GET", "/v1/search"): {"query": "q", "total_results": 0, "results": []},
    ("GET", "/v1/documents"): {"total_documents": 0, "documents": []},
    ("GET", "/v1/collections"): {"total_collections": 0, "collections": []},
    ("GET", "/v1/databases"): {"total_databases": 0, "databases": []},
    ("GET", "/v1/registrations"): {"total_registrations": 0, "registrations": []},
    ("DELETE", "/v1/documents"): {"task_id": "t", "status": "accepted"},
    ("DELETE", "/v1/collections"): {"task_id": "t", "status": "accepted"},
    ("DELETE", "/v1/registrations"): {
        "task_id": "t",
        "status": "accepted",
        "removed": 2,
    },
    ("POST", "/v1/remember"): {"task_id": "t", "status": "accepted"},
    ("POST", "/v1/ingest"): {"task_id": "t", "status": "accepted"},
    ("POST", "/v1/sync"): {"task_id": "t", "status": "accepted"},
    ("POST", "/v1/registrations"): {"task_id": "t", "status": "accepted"},
    ("POST", "/v1/optimize"): {"task_id": "t", "status": "accepted"},
    ("POST", "/v1/backfill-sessions"): {"task_id": "t", "status": "accepted"},
}


@final
class RecordingTransport:
    """Records each request and replays a canned body for its route."""

    __slots__ = ("requests",)

    requests: list[tuple[str, str, dict[str, str], dict[str, object]]]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self.requests = []
        return self

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> Response:
        base = path.split("?")[0]
        self.requests.append((method, base, dict(params or {}), dict(json_body or {})))
        if base.startswith("/v1/tasks/"):
            return Response(
                200, {"task_id": "t", "status": "completed", "results": _TASK_RESULTS}
            )
        if base == "/v1/show":
            return Response(200, self._show_body(params))
        return Response(200, _BODIES.get((method, base), {}))

    @staticmethod
    def _show_body(params: Mapping[str, str] | None) -> dict[str, object]:
        if params and "page" in params:
            return {"document_name": "d", "page_number": 1, "text": "hello"}
        return {
            "document_name": "d",
            "document_path": "/d",
            "collection": "c",
            "total_pages": 1,
            "chunk_count": 1,
            "indexed_pages": 1,
            "ingestion_timestamp": "2026-01-01",
        }

    def params_for(self, method: str, base: str) -> dict[str, str]:
        for m, b, params, _body in self.requests:
            if m == method and b == base:
                return params
        raise AssertionError(f"no {method} {base} request recorded")

    def body_for(self, method: str, base: str) -> dict[str, object]:
        for m, b, _params, body in self.requests:
            if m == method and b == base:
                return body
        raise AssertionError(f"no {method} {base} request recorded")


@pytest.fixture
def transport() -> Iterator[RecordingTransport]:
    """Patch the CLI's client factory to a QuarryClient over a recorder.

    Patches ``TargetResolver.connect`` — the plumbing's actual factory — so the
    command never runs ``resolve()`` and never depends on a live daemon. Patching
    ``QuarryClient.connect`` would leave tier-3 loopback resolution running first,
    making the test pass only where a real quarryd is up (a false green in CI).
    """
    recorder = RecordingTransport()
    with patch.object(TargetResolver, "connect", return_value=QuarryClient(recorder)):
        yield recorder


def _run(args: list[str]) -> object:
    result = runner.invoke(app, ["--json", *args])
    assert result.exit_code == 0, result.output
    return json.loads(result.output) if result.output.strip() else None


class TestFindParamParity:
    def test_every_filter_reaches_the_daemon_query(
        self, transport: RecordingTransport
    ) -> None:
        _run(
            [
                "find",
                "hello",
                "--limit",
                "7",
                "--collection",
                "col",
                "--document",
                "doc",
                "--page-type",
                "code",
                "--source-format",
                ".py",
                "--agent-handle",
                "rmh",
                "--memory-type",
                "fact",
            ]
        )
        params = transport.params_for("GET", "/v1/search")
        # ``q`` is the serialization alias for the query; every filter is present.
        assert params["q"] == "hello"
        assert params["limit"] == "7"
        for key in (
            "collection",
            "document",
            "page_type",
            "source_format",
            "agent_handle",
            "memory_type",
        ):
            assert key in params, f"filter {key} did not reach the daemon"

    def test_empty_filters_are_dropped(self, transport: RecordingTransport) -> None:
        _run(["find", "hello"])
        params = transport.params_for("GET", "/v1/search")
        assert params == {"q": "hello"}


class TestShow:
    def test_page_param_reaches_daemon(self, transport: RecordingTransport) -> None:
        data = _run(["show", "mydoc", "--page", "2", "--collection", "c"])
        params = transport.params_for("GET", "/v1/show")
        assert params["document"] == "mydoc"
        assert params["page"] == "2"
        assert params["collection"] == "c"
        assert isinstance(data, dict)
        assert set(data) == {"document_name", "page_number", "text"}

    def test_document_metadata_fields(self, transport: RecordingTransport) -> None:
        data = _run(["show", "mydoc"])
        assert "page" not in transport.params_for("GET", "/v1/show")
        assert isinstance(data, dict)
        assert "document_path" in data


class TestListFieldParity:
    def test_documents_route(self, transport: RecordingTransport) -> None:
        _run(["list", "documents", "--collection", "c"])
        assert transport.params_for("GET", "/v1/documents")["collection"] == "c"

    def test_collections_route(self, transport: RecordingTransport) -> None:
        _run(["list", "collections"])
        transport.params_for("GET", "/v1/collections")

    def test_registrations_route(self, transport: RecordingTransport) -> None:
        _run(["list", "registrations"])
        transport.params_for("GET", "/v1/registrations")

    def test_databases_route(self, transport: RecordingTransport) -> None:
        _run(["list", "databases"])
        transport.params_for("GET", "/v1/databases")


class TestDelete:
    def test_document_params_and_awaited_count(
        self, transport: RecordingTransport
    ) -> None:
        data = _run(["delete", "mydoc", "--collection", "c"])
        params = transport.params_for("DELETE", "/v1/documents")
        assert params["name"] == "mydoc"
        assert params["collection"] == "c"
        assert isinstance(data, dict)
        assert data["deleted"] == 3

    def test_collection_route(self, transport: RecordingTransport) -> None:
        _run(["delete", "mycol", "--type", "collection"])
        assert transport.params_for("DELETE", "/v1/collections")["name"] == "mycol"


class TestSyncRegisterDeregister:
    def test_register_sends_resolved_dir(
        self, transport: RecordingTransport, tmp_path: Path
    ) -> None:
        _run(["register", str(tmp_path), "--collection", "c"])
        body = transport.body_for("POST", "/v1/registrations")
        assert body["collection"] == "c"
        assert body["directory"]  # an absolute resolved path

    def test_deregister_encodes_params_and_awaits(
        self, transport: RecordingTransport
    ) -> None:
        data = _run(["deregister", "mycol", "--keep-data"])
        params = transport.params_for("DELETE", "/v1/registrations")
        assert params["collection"] == "mycol"
        assert params["keep_data"] == "true"
        assert isinstance(data, dict)
        assert set(data) == {"collection", "removed", "deleted_chunks"}

    def test_sync_posts(self, transport: RecordingTransport) -> None:
        _run(["sync"])
        transport.body_for("POST", "/v1/sync")

    def test_status_route(self, transport: RecordingTransport) -> None:
        with patch.object(
            RecordingTransport,
            "request",
            _status_request,
        ):
            _run(["status"])


class TestMaintenance:
    def test_optimize_sends_force(self, transport: RecordingTransport) -> None:
        _run(["optimize", "--force"])
        assert transport.body_for("POST", "/v1/optimize")["force"] is True

    def test_backfill_forwards_limit(self, transport: RecordingTransport) -> None:
        _run(["backfill-sessions", "--limit", "42", "--collection", "c"])
        body = transport.body_for("POST", "/v1/backfill-sessions")
        assert body["limit"] == 42
        assert body["collection"] == "c"


class TestRemoteList:
    def test_shows_resolved_env_target_not_stale_toml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With QUARRY_URL set, `remote list` must display the ACTUAL resolved
        # target (env-first, like data commands), not a stale quarry.toml url.
        monkeypatch.setenv("QUARRY_URL", "wss://env.example:9000")
        monkeypatch.delenv("QUARRY_TOKEN", raising=False)
        monkeypatch.delenv("QUARRY_CA_CERT", raising=False)
        result = runner.invoke(app, ["remote", "list"])
        assert result.exit_code == 0, result.output
        assert "wss://env.example:9000" in result.output


def _status_request(
    self: RecordingTransport,
    method: str,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    json_body: Mapping[str, object] | None = None,
    timeout: float | None = None,
) -> Response:
    """A status-route override returning a full StatusResponse body."""
    self.requests.append((method, path.split("?")[0], dict(params or {}), {}))
    return Response(
        200,
        {
            "document_count": 0,
            "collection_count": 0,
            "chunk_count": 0,
            "registered_directories": 0,
            "database_path": "/db",
            "database_size_bytes": 0,
            "embedding_model": "m",
            "provider": "cpu",
            "embedding_dimension": 768,
        },
    )
