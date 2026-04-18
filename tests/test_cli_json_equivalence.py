"""Equivalence tests for local/remote JSON shapes in CLI commands.

Covers two categories:
1. Read-only commands: local and remote paths must emit identical JSON keys.
2. Fire-and-forget commands: remote returns ``{task_id, status}``, local
   returns command-specific result keys.  Both shapes must be stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from typer.testing import CliRunner

import quarry.__main__ as cli_mod
from quarry.__main__ import app

runner = CliRunner()

_REMOTE_INNER_CONFIG = {
    "url": "wss://quarry.example.com:8420/mcp",
    "ca_cert": "/path/to/ca.crt",
    "headers": {"Authorization": "Bearer tok"},
}
_REMOTE_PROXY_CONFIG = {"quarry": _REMOTE_INNER_CONFIG}


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    return s


def _mock_registration(collection: str = "math") -> MagicMock:
    reg = MagicMock()
    reg.collection = collection
    reg.directory = f"/home/u/{collection}"
    reg.registered_at = "2026-01-01T00:00:00"
    return reg


def _reset_globals() -> None:
    cli_mod._json_output = False
    cli_mod._verbose = False
    cli_mod._quiet = False
    cli_mod._global_db = ""


# ---------------------------------------------------------------------------
# Read-only commands: full key-set equivalence
# ---------------------------------------------------------------------------


class TestJsonEquivalenceFind:
    """``quarry find`` local and remote JSON shapes must match."""

    def test_json_equivalence_remote_local_find(self) -> None:
        result_fields = {
            "document_name": "doc.pdf",
            "collection": "default",
            "page_number": 3,
            "chunk_index": 7,
            "page_type": "body",
            "source_format": ".pdf",
            "agent_handle": "rmh",
            "memory_type": "fact",
            "summary": "a summary",
            "text": "full text here",
            "similarity": 0.85,
        }

        # Remote path
        remote_response = {"results": [result_fields]}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_get",
                return_value=remote_response,
            ),
        ):
            remote_res = runner.invoke(app, ["--json", "find", "query"])
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_data = json.loads(remote_res.output)
        assert len(remote_data) == 1
        remote_keys = set(remote_data[0].keys())

        # Local path
        local_row = {
            "document_name": "doc.pdf",
            "collection": "default",
            "page_number": 3,
            "chunk_index": 7,
            "page_type": "body",
            "source_format": ".pdf",
            "agent_handle": "rmh",
            "memory_type": "fact",
            "summary": "a summary",
            "text": "full text here",
            "_distance": 0.15,
        }
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch(
                "quarry.__main__.hybrid_search",
                return_value=[local_row],
            ),
        ):
            local_res = runner.invoke(app, ["--json", "find", "query"])
        _reset_globals()
        assert local_res.exit_code == 0
        local_data = json.loads(local_res.output)
        assert len(local_data) == 1
        local_keys = set(local_data[0].keys())

        assert remote_keys == local_keys, (
            f"Field mismatch: remote-only={remote_keys - local_keys}, "
            f"local-only={local_keys - remote_keys}"
        )


class TestJsonEquivalenceStatus:
    """``quarry status`` local and remote JSON shapes must match."""

    def test_json_equivalence_remote_local_status(self) -> None:
        status_fields = {
            "document_count": 5,
            "collection_count": 2,
            "chunk_count": 100,
            "registered_directories": 1,
            "database_path": "/data/lancedb",
            "database_size_bytes": 4096,
            "embedding_model": "snowflake-arctic-embed-m-v1.5",
            "provider": "cpu",
            "embedding_dimension": 768,
        }

        # Remote path
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_get",
                return_value=status_fields,
            ),
        ):
            remote_res = runner.invoke(app, ["--json", "status"])
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_data = json.loads(remote_res.output)
        remote_keys = set(remote_data.keys())

        # Local path
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = False
        mock_settings.lancedb_path.exists.return_value = False
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=mock_settings,
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                return_value=[{"doc": 1}] * 5,
            ),
            patch("quarry.__main__.count_chunks", return_value=100),
            patch(
                "quarry.__main__.db_list_collections",
                return_value=[{"c": 1}, {"c": 2}],
            ),
            patch("quarry.__main__.provider_display", return_value="cpu"),
        ):
            local_res = runner.invoke(app, ["--json", "status"])
        _reset_globals()
        assert local_res.exit_code == 0
        local_data = json.loads(local_res.output)
        local_keys = set(local_data.keys())

        assert remote_keys == local_keys, (
            f"Field mismatch: remote-only={remote_keys - local_keys}, "
            f"local-only={local_keys - remote_keys}"
        )


# ---------------------------------------------------------------------------
# Fire-and-forget commands: shape tests
# ---------------------------------------------------------------------------


class TestJsonShapeIngest:
    """``quarry ingest`` remote and local JSON shapes."""

    def test_json_remote_returns_task_shape_ingest(self) -> None:
        remote_resp = {"task_id": "ingest-t99", "status": "accepted"}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                return_value=remote_resp,
            ),
        ):
            result = runner.invoke(app, ["--json", "ingest", "https://example.com/"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data.keys()) == {"task_id", "status"}
        assert isinstance(data["task_id"], str)
        assert data["status"] == "accepted"

    def test_json_local_returns_result_shape_ingest(self) -> None:
        local_result = {
            "document_name": "example.com",
            "chunks": 5,
            "pages": 2,
            "errors": [],
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_auto",
                return_value=local_result,
            ),
        ):
            result = runner.invoke(app, ["--json", "ingest", "https://example.com/"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "document_name" in data
        assert "chunks" in data
        assert "errors" in data


class TestJsonShapeRemember:
    """``quarry remember`` remote and local JSON shapes."""

    def test_json_remote_returns_task_shape_remember(self) -> None:
        remote_resp = {"task_id": "remember-t88", "status": "accepted"}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                return_value=remote_resp,
            ),
        ):
            result = runner.invoke(
                app,
                ["--json", "remember", "--name", "n.md"],
                input="body",
            )
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data.keys()) == {"task_id", "status"}
        assert isinstance(data["task_id"], str)
        assert data["status"] == "accepted"

    def test_json_local_returns_result_shape_remember(self) -> None:
        local_result = {
            "document_name": "n.md",
            "chunks": 1,
            "pages": 1,
            "errors": [],
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value=local_result,
            ),
        ):
            result = runner.invoke(
                app,
                ["--json", "remember", "--name", "n.md"],
                input="body",
            )
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "document_name" in data
        assert "chunks" in data
        assert "errors" in data


class TestJsonShapeDelete:
    """``quarry delete`` remote and local JSON shapes."""

    def test_json_remote_returns_task_shape_delete(self) -> None:
        remote_resp = {"task_id": "delete-t77", "status": "accepted"}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                return_value=remote_resp,
            ),
        ):
            result = runner.invoke(app, ["--json", "delete", "report.pdf"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data.keys()) == {"task_id", "status"}
        assert isinstance(data["task_id"], str)
        assert data["status"] == "accepted"

    def test_json_local_returns_result_shape_delete(self) -> None:
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=10),
        ):
            result = runner.invoke(app, ["--json", "delete", "report.pdf"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data.keys()) == {"deleted", "name", "type"}
        assert data["deleted"] == 10
        assert data["name"] == "report.pdf"
        assert data["type"] == "document"


class TestJsonShapeRegister:
    """``quarry register`` remote and local JSON shapes."""

    def test_json_remote_returns_task_shape_register(self, tmp_path: Path) -> None:
        d = tmp_path / "docs"
        d.mkdir()
        remote_resp = {"task_id": "register-t66", "status": "accepted"}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                return_value=remote_resp,
            ),
        ):
            result = runner.invoke(
                app,
                ["--json", "register", str(d), "--collection", "docs"],
            )
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data.keys()) == {"task_id", "status"}
        assert isinstance(data["task_id"], str)
        assert data["status"] == "accepted"

    def test_json_local_returns_result_shape_register(self, tmp_path: Path) -> None:
        d = tmp_path / "docs"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=settings,
            ),
        ):
            result = runner.invoke(
                app,
                ["--json", "register", str(d), "--collection", "docs"],
            )
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "directory" in data
        assert "collection" in data


class TestJsonShapeDeregister:
    """``quarry deregister`` remote and local JSON shapes."""

    def test_json_remote_returns_task_shape_deregister(self) -> None:
        remote_resp = {"task_id": "deregister-t55", "status": "accepted"}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                return_value=remote_resp,
            ),
        ):
            result = runner.invoke(app, ["--json", "deregister", "math"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert set(data.keys()) == {"task_id", "status"}
        assert isinstance(data["task_id"], str)
        assert data["status"] == "accepted"

    def test_json_local_returns_result_shape_deregister(self, tmp_path: Path) -> None:
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=settings,
            ),
            patch(
                "quarry.__main__.get_registration",
                return_value=_mock_registration("math"),
            ),
            patch(
                "quarry.__main__.deregister_directory",
                return_value=["a", "b"],
            ),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=3),
        ):
            result = runner.invoke(app, ["--json", "deregister", "math"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "collection" in data
        assert "removed" in data
        assert "deleted_chunks" in data


class TestJsonShapeSync:
    """``quarry sync`` remote and local JSON shapes."""

    def test_json_remote_returns_task_shape_sync(self) -> None:
        remote_resp = {"task_id": "sync-t44", "status": "accepted"}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                return_value=remote_resp,
            ),
        ):
            result = runner.invoke(app, ["--json", "sync"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "task_id" in data
        assert data["status"] == "accepted"

    def test_json_local_returns_result_shape_sync(self) -> None:
        from quarry.sync import SyncResult

        mock_results = {
            "col": SyncResult(
                collection="col",
                ingested=2,
                refreshed=0,
                deleted=1,
                skipped=0,
                failed=0,
                errors=[],
            )
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.sync_all",
                return_value=mock_results,
            ),
        ):
            result = runner.invoke(app, ["--json", "sync"])
        _reset_globals()
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "col" in data
        entry_keys = set(data["col"].keys())
        expected_keys = {
            "ingested",
            "refreshed",
            "deleted",
            "skipped",
            "failed",
            "errors",
        }
        assert entry_keys == expected_keys, (
            f"Key mismatch: extra={entry_keys - expected_keys}, "
            f"missing={expected_keys - entry_keys}"
        )
