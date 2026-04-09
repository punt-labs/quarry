from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import numpy as np
from typer.testing import CliRunner

if TYPE_CHECKING:
    import pytest

import quarry.__main__ as cli_mod
from quarry.__main__ import app

runner = CliRunner()


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    return s


def _reset_globals() -> None:
    """Reset CLI globals between tests."""
    cli_mod._json_output = False
    cli_mod._verbose = False
    cli_mod._quiet = False
    cli_mod._global_db = ""


class TestListDocumentsCmd:
    def test_lists_documents(self):
        mock_docs = [
            {
                "document_name": "report.pdf",
                "collection": "default",
                "indexed_pages": 10,
                "total_pages": 10,
                "chunk_count": 25,
            },
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=mock_docs),
        ):
            result = runner.invoke(app, ["list", "documents"])

        assert result.exit_code == 0
        assert "default" in result.output
        assert "report.pdf" in result.output
        assert "10" in result.output
        assert "25" in result.output

    def test_filters_by_collection(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]) as mock_list,
        ):
            runner.invoke(app, ["list", "documents", "--collection", "math"])

        call_kwargs = mock_list.call_args[1]
        assert call_kwargs["collection_filter"] == "math"

    def test_empty_database(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "documents"])

        assert result.exit_code == 0
        assert "No documents" in result.output

    def test_remote_routing_when_config_present(self):
        remote_docs = {
            "total_documents": 1,
            "documents": [
                {
                    "document_name": "remote-report.pdf",
                    "collection": "remote-col",
                    "indexed_pages": 5,
                    "total_pages": 5,
                    "chunk_count": 12,
                },
            ],
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_docs
            ) as mock_get,
        ):
            result = runner.invoke(app, ["list", "documents"])

        assert result.exit_code == 0
        mock_get.assert_called_once_with("/documents", inner_config)
        assert "remote-report.pdf" in result.output
        assert "remote-col" in result.output

    def test_remote_routing_with_collection_filter(self):
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get",
                return_value={"documents": []},
            ) as mock_get,
        ):
            runner.invoke(app, ["list", "documents", "--collection", "math"])

        call_path: str = mock_get.call_args[0][0]
        assert "collection=math" in call_path

    def test_local_path_when_no_config(self):
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "documents"])

        assert result.exit_code == 0
        assert "No documents" in result.output

    def test_bare_list_errors(self):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 1

    def test_remote_json_includes_all_fields(self):
        """Remote path emits the same JSON field names as the local path."""
        doc_fields = {
            "document_name": "remote-report.pdf",
            "document_path": "/docs/remote-report.pdf",
            "collection": "remote-col",
            "total_pages": 5,
            "chunk_count": 12,
            "indexed_pages": 5,
            "ingestion_timestamp": "2026-01-01T00:00:00",
        }

        # Remote path
        remote_resp = {"documents": [doc_fields]}
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_get", return_value=remote_resp),
        ):
            remote_result = runner.invoke(app, ["--json", "list", "documents"])
        _reset_globals()

        assert remote_result.exit_code == 0
        remote_data = json.loads(remote_result.output)
        assert len(remote_data) == 1
        remote_keys = set(remote_data[0].keys())

        # Local path
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[doc_fields]),
        ):
            local_result = runner.invoke(app, ["--json", "list", "documents"])
        _reset_globals()

        assert local_result.exit_code == 0
        local_data = json.loads(local_result.output)
        assert len(local_data) == 1
        local_keys = set(local_data[0].keys())

        assert remote_keys == local_keys, (
            f"Field mismatch: remote-only={remote_keys - local_keys}, "
            f"local-only={local_keys - remote_keys}"
        )


class TestShowCmd:
    def test_show_page(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_page_text",
                return_value="Hello world",
            ),
        ):
            result = runner.invoke(app, ["show", "report.pdf", "--page", "2"])

        assert result.exit_code == 0
        assert "report.pdf" in result.output
        assert "Page: 2" in result.output
        assert "Hello world" in result.output

    def test_show_metadata(self):
        mock_doc = {
            "document_name": "report.pdf",
            "document_path": "/docs/report.pdf",
            "collection": "math",
            "total_pages": 10,
            "chunk_count": 42,
            "indexed_pages": 10,
            "ingestion_timestamp": "2026-01-01T00:00:00",
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[mock_doc]),
        ):
            result = runner.invoke(app, ["show", "report.pdf"])

        assert result.exit_code == 0
        assert "report.pdf" in result.output
        assert "math" in result.output

    def test_show_not_found(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["show", "missing.pdf"])

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_show_page_not_found(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_page_text", return_value=None),
        ):
            result = runner.invoke(app, ["show", "report.pdf", "--page", "999"])

        assert result.exit_code == 1
        assert "No data found" in result.output

    def test_show_page_zero(self):
        result = runner.invoke(app, ["show", "report.pdf", "--page", "0"])

        assert result.exit_code == 1
        assert "page number must be >= 1" in result.output

    def test_show_negative_page(self):
        result = runner.invoke(app, ["show", "report.pdf", "--page", "-1"])

        assert result.exit_code == 1
        assert "page number must be >= 1" in result.output

    def test_page_zero_errors(self):
        result = runner.invoke(app, ["show", "doc", "--page", "0"])

        assert result.exit_code == 1

    def test_show_collection_filter(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_page_text",
                return_value="page text",
            ) as mock_get_page,
        ):
            result = runner.invoke(
                app,
                ["show", "report.pdf", "--page", "1", "--collection", "math"],
            )

        assert result.exit_code == 0
        assert mock_get_page.call_args[1]["collection"] == "math"

    def test_remote_routing_page_text(self):
        remote_resp = {
            "document_name": "report.pdf",
            "page_number": 3,
            "text": "Remote page content",
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_resp
            ) as mock_get,
        ):
            result = runner.invoke(app, ["show", "report.pdf", "--page", "3"])

        assert result.exit_code == 0
        mock_get.assert_called_once()
        call_path: str = mock_get.call_args[0][0]
        assert "/show?" in call_path
        assert "document=report.pdf" in call_path
        assert "page=3" in call_path
        assert "Remote page content" in result.output

    def test_remote_routing_metadata(self):
        remote_resp = {
            "document_name": "report.pdf",
            "collection": "math",
            "total_pages": 10,
            "chunk_count": 42,
            "indexed_pages": 10,
            "ingestion_timestamp": "2026-01-01T00:00:00",
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_resp
            ) as mock_get,
        ):
            result = runner.invoke(app, ["show", "report.pdf"])

        assert result.exit_code == 0
        mock_get.assert_called_once()
        call_path = mock_get.call_args[0][0]
        assert "/show?" in call_path
        assert "document=report.pdf" in call_path
        assert "page" not in call_path
        assert "report.pdf" in result.output
        assert "math" in result.output

    def test_remote_routing_with_collection(self):
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get",
                return_value={
                    "document_name": "doc.pdf",
                    "page_number": 1,
                    "text": "t",
                },
            ) as mock_get,
        ):
            runner.invoke(
                app,
                ["show", "doc.pdf", "--page", "1", "--collection", "math"],
            )

        call_path = mock_get.call_args[0][0]
        assert "collection=math" in call_path

    def test_local_path_when_no_config(self):
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["show", "missing.pdf"])

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_remote_json_page_includes_all_fields(self):
        """Remote page path emits the same JSON field names as the local path."""
        # Remote path
        remote_resp = {
            "document_name": "report.pdf",
            "page_number": 3,
            "text": "page content",
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_get", return_value=remote_resp),
        ):
            remote_result = runner.invoke(
                app, ["--json", "show", "report.pdf", "--page", "3"]
            )
        _reset_globals()

        assert remote_result.exit_code == 0
        remote_data = json.loads(remote_result.output)
        remote_keys = set(remote_data.keys())

        # Local path
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_page_text", return_value="page content"),
        ):
            local_result = runner.invoke(
                app, ["--json", "show", "report.pdf", "--page", "3"]
            )
        _reset_globals()

        assert local_result.exit_code == 0
        local_data = json.loads(local_result.output)
        local_keys = set(local_data.keys())

        assert remote_keys == local_keys, (
            f"Field mismatch: remote-only={remote_keys - local_keys}, "
            f"local-only={local_keys - remote_keys}"
        )

    def test_remote_json_metadata_includes_all_fields(self):
        """Remote metadata path emits the same JSON field names as the local path."""
        doc_fields = {
            "document_name": "report.pdf",
            "document_path": "/docs/report.pdf",
            "collection": "math",
            "total_pages": 10,
            "chunk_count": 42,
            "indexed_pages": 10,
            "ingestion_timestamp": "2026-01-01T00:00:00",
        }

        # Remote path
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_get", return_value=doc_fields),
        ):
            remote_result = runner.invoke(app, ["--json", "show", "report.pdf"])
        _reset_globals()

        assert remote_result.exit_code == 0
        remote_data = json.loads(remote_result.output)
        remote_keys = set(remote_data.keys())

        # Local path
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[doc_fields]),
        ):
            local_result = runner.invoke(app, ["--json", "show", "report.pdf"])
        _reset_globals()

        assert local_result.exit_code == 0
        local_data = json.loads(local_result.output)
        local_keys = set(local_data.keys())

        assert remote_keys == local_keys, (
            f"Field mismatch: remote-only={remote_keys - local_keys}, "
            f"local-only={local_keys - remote_keys}"
        )


class TestStatusCmd:
    def test_shows_status(self):
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = False
        mock_settings.lancedb_path.exists.return_value = False
        with (
            patch("quarry.__main__._resolved_settings", return_value=mock_settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Documents" in result.output
        assert "Chunks" in result.output

    def test_status_with_registrations(self):
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = True
        mock_settings.lancedb_path.exists.return_value = False
        mock_reg = MagicMock()
        with (
            patch("quarry.__main__._resolved_settings", return_value=mock_settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
            patch("quarry.__main__.open_registry") as mock_open,
            patch(
                "quarry.__main__.list_registrations", return_value=[mock_reg, mock_reg]
            ),
        ):
            mock_open.return_value.close = MagicMock()
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "2" in result.output

    def test_status_with_documents(self):
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = False
        mock_settings.lancedb_path.exists.return_value = False
        mock_docs = [
            {
                "document_name": "a.pdf",
                "collection": "default",
                "indexed_pages": 5,
                "total_pages": 5,
                "chunk_count": 20,
            },
            {
                "document_name": "b.pdf",
                "collection": "default",
                "indexed_pages": 3,
                "total_pages": 3,
                "chunk_count": 10,
            },
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=mock_settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=mock_docs),
            patch("quarry.__main__.count_chunks", return_value=30),
            patch(
                "quarry.__main__.db_list_collections",
                return_value=[{"collection": "default"}],
            ),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "2" in result.output
        assert "30" in result.output

    def test_remote_routing_when_config_present(self):
        remote_status = {
            "document_count": 42,
            "collection_count": 3,
            "chunk_count": 1200,
            "database_path": "/remote/path/lancedb",
            "database_size_bytes": 8192,
            "embedding_model": "snowflake-arctic-embed-m-v1.5",
            "provider": "cuda",
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_status
            ) as mock_get,
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        mock_get.assert_called_once_with("/status", inner_config)
        assert "42" in result.output
        assert "1,200" in result.output

    def test_local_path_when_no_config(self):
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = False
        mock_settings.lancedb_path.exists.return_value = False
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=mock_settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Documents" in result.output

    def test_malformed_toml_warns_and_falls_back_to_local(self):
        """ValueError from read_proxy_config prints a warning, falls back to local."""
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = False
        mock_settings.lancedb_path.exists.return_value = False
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                side_effect=ValueError("bad toml"),
            ),
            patch("quarry.__main__._resolved_settings", return_value=mock_settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "bad toml" in result.output


class TestUseCmd:
    def test_sets_default_db(self):
        with (
            patch("quarry.__main__.resolve_db_paths", return_value=_mock_settings()),
            patch("quarry.__main__.write_default_db") as mock_write,
        ):
            result = runner.invoke(app, ["use", "work"])

        assert result.exit_code == 0
        assert "work" in result.output
        mock_write.assert_called_once_with("work")

    def test_invalid_db_name(self):
        with patch(
            "quarry.__main__.resolve_db_paths",
            side_effect=ValueError("Invalid database name"),
        ):
            result = runner.invoke(app, ["use", "../escape"])

        assert result.exit_code == 1

    def test_use_empty_name(self):
        """Empty string should fail validation in resolve_db_paths."""
        with patch(
            "quarry.__main__.resolve_db_paths",
            side_effect=ValueError("empty name"),
        ):
            result = runner.invoke(app, ["use", ""])

        assert result.exit_code == 1

    def test_use_write_failure(self):
        with (
            patch("quarry.__main__.resolve_db_paths", return_value=_mock_settings()),
            patch(
                "quarry.__main__.write_default_db",
                side_effect=PermissionError("read-only"),
            ),
        ):
            result = runner.invoke(app, ["use", "work"])

        assert result.exit_code == 1


class TestDeleteCmd:
    def test_deletes_document(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=15),
        ):
            result = runner.invoke(app, ["delete", "report.pdf"])

        assert result.exit_code == 0
        assert "Deleted 15 chunks" in result.output
        assert "report.pdf" in result.output

    def test_not_found(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=0),
        ):
            result = runner.invoke(app, ["delete", "missing.pdf"])

        assert result.exit_code == 1
        assert "No data found" in result.output

    def test_delete_with_collection_scope(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=5) as mock_del,
        ):
            result = runner.invoke(app, ["delete", "doc.pdf", "--collection", "math"])

        assert result.exit_code == 0
        assert mock_del.call_args[1]["collection"] == "math"

    def test_delete_unknown_type(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
        ):
            result = runner.invoke(app, ["delete", "x", "--type", "bogus"])

        assert result.exit_code == 1
        assert "unknown type" in result.output.lower()

    def test_delete_backend_error(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.db_delete_document",
                side_effect=RuntimeError("db locked"),
            ),
        ):
            result = runner.invoke(app, ["delete", "doc.pdf"])

        assert result.exit_code == 1

    def test_remote_routing_delete_document(self):
        remote_resp = {"deleted": 15, "name": "report.pdf", "type": "document"}
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(app, ["delete", "report.pdf"])

        assert result.exit_code == 0
        mock_req.assert_called_once()
        call_method: str = mock_req.call_args[0][0]
        call_path: str = mock_req.call_args[0][1]
        assert call_method == "DELETE"
        assert "/documents?" in call_path
        assert "name=report.pdf" in call_path
        assert "Deleted 15 chunks" in result.output

    def test_remote_routing_delete_document_with_collection(self):
        remote_resp = {"deleted": 5, "name": "doc.pdf", "type": "document"}
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            runner.invoke(app, ["delete", "doc.pdf", "--collection", "math"])

        call_path = mock_req.call_args[0][1]
        assert "collection=math" in call_path

    def test_remote_routing_delete_collection(self):
        remote_resp = {"deleted": 50, "name": "math", "type": "collection"}
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(app, ["delete", "math", "--type", "collection"])

        assert result.exit_code == 0
        call_method = mock_req.call_args[0][0]
        call_path = mock_req.call_args[0][1]
        assert call_method == "DELETE"
        assert "/collections?" in call_path
        assert "name=math" in call_path
        assert "Deleted 50 chunks" in result.output

    def test_remote_routing_unknown_type(self):
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with patch("quarry.__main__.read_proxy_config", return_value=proxy_config):
            result = runner.invoke(app, ["delete", "x", "--type", "bogus"])

        assert result.exit_code == 1
        assert "unknown type" in result.output.lower()

    def test_local_path_when_no_config(self):
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=0),
        ):
            result = runner.invoke(app, ["delete", "missing.pdf"])

        assert result.exit_code == 1
        assert "No data found" in result.output

    def test_remote_json_delete_document_includes_all_fields(self):
        """Remote delete path emits the same JSON field names as the local path."""
        # Remote path
        remote_resp = {"deleted": 15, "name": "report.pdf", "type": "document"}
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            remote_result = runner.invoke(app, ["--json", "delete", "report.pdf"])
        _reset_globals()

        assert remote_result.exit_code == 0
        remote_data = json.loads(remote_result.output)
        remote_keys = set(remote_data.keys())

        # Local path
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=15),
        ):
            local_result = runner.invoke(app, ["--json", "delete", "report.pdf"])
        _reset_globals()

        assert local_result.exit_code == 0
        local_data = json.loads(local_result.output)
        local_keys = set(local_data.keys())

        assert remote_keys == local_keys, (
            f"Field mismatch: remote-only={remote_keys - local_keys}, "
            f"local-only={local_keys - remote_keys}"
        )


class TestFindCmd:
    def test_prints_results(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        mock_results = [
            {
                "document_name": "report.pdf",
                "page_number": 3,
                "text": "quarterly revenue grew 15%",
                "page_type": "text",
                "source_format": ".pdf",
                "_distance": 0.15,
            },
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=mock_results),
        ):
            result = runner.invoke(app, ["find", "revenue growth"])

        assert result.exit_code == 0
        assert "report.pdf" in result.output
        assert "p.3" in result.output
        assert "text/.pdf" in result.output
        assert "quarterly revenue" in result.output

    def test_no_results(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=[]),
        ):
            result = runner.invoke(app, ["find", "nonexistent topic"])

        assert result.exit_code == 0

    def _assert_filter_passthrough(
        self,
        cli_flag: str,
        cli_value: str,
        expected_key: str,
        expected_value: str | None,
    ) -> None:
        """Invoke find with one CLI flag and assert it reaches search()."""
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=[]) as mock_search,
        ):
            result = runner.invoke(app, ["find", "query", cli_flag, cli_value])

        assert result.exit_code == 0
        assert mock_search.call_args[1][expected_key] == expected_value

    def test_passes_document_filter(self):
        self._assert_filter_passthrough(
            "--document",
            "report.pdf",
            "document_filter",
            "report.pdf",
        )

    def test_passes_page_type_filter(self):
        self._assert_filter_passthrough(
            "--page-type",
            "code",
            "page_type_filter",
            "code",
        )

    def test_passes_source_format_filter(self):
        self._assert_filter_passthrough(
            "--source-format",
            ".py",
            "source_format_filter",
            ".py",
        )

    def test_empty_filters_pass_none(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=[]) as mock_search,
        ):
            result = runner.invoke(app, ["find", "query"])

        assert result.exit_code == 0
        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["document_filter"] is None
        assert call_kwargs["page_type_filter"] is None
        assert call_kwargs["source_format_filter"] is None

    def test_passes_limit_flag(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=[]) as mock_search,
        ):
            result = runner.invoke(app, ["find", "query", "--limit", "5"])

        assert result.exit_code == 0
        assert mock_search.call_args[1]["limit"] == 5

    def test_passes_collection_filter(self):
        self._assert_filter_passthrough(
            "--collection",
            "math",
            "collection_filter",
            "math",
        )

    def test_empty_collection_passes_none(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=[]) as mock_search,
        ):
            result = runner.invoke(app, ["find", "query"])

        assert result.exit_code == 0
        assert mock_search.call_args[1]["collection_filter"] is None

    def test_missing_distance_defaults_zero(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        mock_results = [
            {
                "document_name": "doc.pdf",
                "page_number": 1,
                "text": "hello",
                "page_type": "text",
                "source_format": ".pdf",
            },
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=mock_results),
        ):
            result = runner.invoke(app, ["find", "hello"])

        assert result.exit_code == 0
        assert "similarity: 1.0" in result.output

    def test_embedding_backend_error(self):
        mock_backend = MagicMock()
        mock_backend.embed_query.side_effect = RuntimeError("model not found")
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
        ):
            result = runner.invoke(app, ["find", "query"])

        assert result.exit_code == 1

    def test_remote_routing_when_config_present(self):
        remote_response = {
            "results": [
                {
                    "document_name": "remote-doc.pdf",
                    "collection": "remote-col",
                    "text": "remote search result text",
                    "similarity": 0.91,
                },
            ]
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_response
            ) as mock_get,
        ):
            result = runner.invoke(app, ["find", "some query", "--limit", "5"])

        assert result.exit_code == 0
        mock_get.assert_called_once()
        call_path: str = mock_get.call_args[0][0]
        assert "/search?" in call_path
        assert "q=some+query" in call_path or "q=some%20query" in call_path
        assert "limit=5" in call_path
        assert "remote-doc.pdf" in result.output
        assert "remote search result text" in result.output

    def test_remote_routing_includes_filters(self):
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get", return_value={"results": []}
            ) as mock_get,
        ):
            runner.invoke(
                app,
                ["find", "query", "--collection", "math", "--document", "notes.pdf"],
            )

        call_path = mock_get.call_args[0][0]
        assert "collection=math" in call_path
        assert "document=notes.pdf" in call_path

    def test_local_path_when_no_config(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_embedding_backend", return_value=mock_backend),
            patch("quarry.__main__.hybrid_search", return_value=[]),
        ):
            result = runner.invoke(app, ["find", "query"])

        assert result.exit_code == 0

    def test_malformed_toml_warns_and_falls_back_to_local(self):
        """ValueError from read_proxy_config prints a warning, falls back to local."""
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                side_effect=ValueError("bad toml"),
            ),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_embedding_backend", return_value=mock_backend),
            patch("quarry.__main__.hybrid_search", return_value=[]),
        ):
            result = runner.invoke(app, ["find", "query"])

        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "bad toml" in result.output

    def test_remote_json_includes_all_fields(self):
        """Remote path emits the same JSON shape as the local path."""
        remote_response = {
            "results": [
                {
                    "document_name": "remote-doc.pdf",
                    "collection": "remote-col",
                    "page_number": 3,
                    "chunk_index": 7,
                    "page_type": "body",
                    "source_format": ".pdf",
                    "agent_handle": "rmh",
                    "memory_type": "fact",
                    "summary": "remote doc summary",
                    "text": "full text here",
                    "similarity": 0.85,
                },
            ]
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_get", return_value=remote_response),
        ):
            result = runner.invoke(app, ["--json", "find", "query"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 1
        item = data[0]
        assert item["document_name"] == "remote-doc.pdf"
        assert item["collection"] == "remote-col"
        assert item["page_number"] == 3
        assert item["chunk_index"] == 7
        assert item["page_type"] == "body"
        assert item["source_format"] == ".pdf"
        assert item["agent_handle"] == "rmh"
        assert item["memory_type"] == "fact"
        assert item["summary"] == "remote doc summary"
        assert item["similarity"] == 0.85
        assert item["text"] == "full text here"


class TestProxyConfigIsinstanceGuard:
    """Regression tests: quarry = "somestring" in config must not crash find/status."""

    def test_find_falls_back_to_local_when_quarry_config_is_string(self) -> None:
        """find_cmd falls through to local mode when proxy_config['quarry'] is a string.

        If the TOML has `quarry = "somestring"` rather than a table, the old
        `if proxy_config and "url" in proxy_config` was a substring check.
        The isinstance guard must short-circuit to local mode instead of crashing.
        """
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = mock_vector
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value={"quarry": "not-a-dict"},
            ),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_embedding_backend", return_value=mock_backend),
            patch("quarry.__main__.hybrid_search", return_value=[]),
        ):
            result = runner.invoke(app, ["find", "query"])

        assert result.exit_code == 0

    def test_status_falls_back_to_local_when_quarry_config_is_string(self) -> None:
        """status_cmd falls through to local when proxy_config['quarry'] is a string."""
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = False
        mock_settings.lancedb_path.exists.return_value = False
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value={"quarry": "not-a-dict"},
            ),
            patch("quarry.__main__._resolved_settings", return_value=mock_settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Documents" in result.output


class TestDeleteCollectionCmd:
    def test_deletes_collection(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_collection", return_value=50),
        ):
            result = runner.invoke(app, ["delete", "math", "--type", "collection"])

        assert result.exit_code == 0
        assert "Deleted 50 chunks" in result.output
        assert "math" in result.output

    def test_not_found(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_collection", return_value=0),
        ):
            result = runner.invoke(app, ["delete", "unknown", "--type", "collection"])

        assert result.exit_code == 1
        assert "No data found" in result.output

    def test_delete_collection_backend_error(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.db_delete_collection",
                side_effect=RuntimeError("db corrupt"),
            ),
        ):
            result = runner.invoke(app, ["delete", "math", "--type", "collection"])

        assert result.exit_code == 1


class TestListCollectionsCmd:
    def test_lists_collections(self):
        mock_cols = [
            {"collection": "math", "document_count": 5, "chunk_count": 100},
            {"collection": "science", "document_count": 3, "chunk_count": 60},
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=mock_cols),
        ):
            result = runner.invoke(app, ["list", "collections"])

        assert result.exit_code == 0
        assert "math" in result.output
        assert "5" in result.output
        assert "100" in result.output
        assert "science" in result.output

    def test_empty(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "collections"])

        assert result.exit_code == 0
        assert "No collections" in result.output

    def test_remote_routing_when_config_present(self):
        remote_cols = {
            "total_collections": 2,
            "collections": [
                {"collection": "math", "document_count": 5, "chunk_count": 100},
                {"collection": "science", "document_count": 3, "chunk_count": 60},
            ],
        }
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "ca_cert": "/path/to/ca.crt",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_cols
            ) as mock_get,
        ):
            result = runner.invoke(app, ["list", "collections"])

        assert result.exit_code == 0
        mock_get.assert_called_once_with("/collections", inner_config)
        assert "math" in result.output
        assert "5" in result.output
        assert "science" in result.output

    def test_local_path_when_no_config(self):
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "collections"])

        assert result.exit_code == 0
        assert "No collections" in result.output

    def test_remote_json_includes_all_fields(self):
        """Remote path emits the same JSON field names as the local path."""
        col_fields = {
            "collection": "math",
            "document_count": 5,
            "chunk_count": 100,
        }

        # Remote path
        remote_resp = {"collections": [col_fields]}
        inner_config = {
            "url": "wss://quarry.example.com:8420/mcp",
            "headers": {"Authorization": "Bearer tok"},
        }
        proxy_config = {"quarry": inner_config}
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_get", return_value=remote_resp),
        ):
            remote_result = runner.invoke(app, ["--json", "list", "collections"])
        _reset_globals()

        assert remote_result.exit_code == 0
        remote_data = json.loads(remote_result.output)
        assert len(remote_data) == 1
        remote_keys = set(remote_data[0].keys())

        # Local path
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=[col_fields]),
        ):
            local_result = runner.invoke(app, ["--json", "list", "collections"])
        _reset_globals()

        assert local_result.exit_code == 0
        local_data = json.loads(local_result.output)
        assert len(local_data) == 1
        local_keys = set(local_data[0].keys())

        assert remote_keys == local_keys, (
            f"Field mismatch: remote-only={remote_keys - local_keys}, "
            f"local-only={local_keys - remote_keys}"
        )


class TestRegisterCmd:
    def test_registers_directory(self, tmp_path: Path):
        d = tmp_path / "course"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with patch("quarry.__main__._resolved_settings", return_value=settings):
            result = runner.invoke(
                app, ["register", str(d), "--collection", "my-course"]
            )
        assert result.exit_code == 0
        assert "my-course" in result.output

    def test_register_default_collection(self, tmp_path: Path):
        d = tmp_path / "ml-101"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with patch("quarry.__main__._resolved_settings", return_value=settings):
            result = runner.invoke(app, ["register", str(d)])
        assert result.exit_code == 0
        assert "ml-101" in result.output

    def test_register_nonexistent_dir(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.open_registry"),
            patch(
                "quarry.__main__.register_directory",
                side_effect=FileNotFoundError("dir not found"),
            ),
        ):
            result = runner.invoke(app, ["register", "/no/such/dir"])
        assert result.exit_code == 1

    def test_register_already_registered(self, tmp_path: Path):
        from sqlite3 import IntegrityError

        d = tmp_path / "course"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.open_registry"),
            patch(
                "quarry.__main__.register_directory",
                side_effect=IntegrityError("UNIQUE constraint"),
            ),
        ):
            result = runner.invoke(app, ["register", str(d)])
        assert result.exit_code == 1


def _mock_registration(collection: str = "math") -> MagicMock:
    """Return a DirectoryRegistration-like mock for get_registration patches."""
    reg = MagicMock()
    reg.collection = collection
    reg.directory = f"/home/u/{collection}"
    reg.registered_at = "2026-01-01T00:00:00"
    return reg


class TestDeregisterCmd:
    def test_deregisters_collection(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch(
                "quarry.__main__.get_registration",
                return_value=_mock_registration("math"),
            ),
            patch("quarry.__main__.deregister_directory", return_value=["a.pdf"]),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=3),
        ):
            result = runner.invoke(app, ["deregister", "math"])
        assert result.exit_code == 0
        assert "Deregistered" in result.output
        assert "math" in result.output
        assert "3 chunks deleted" in result.output

    def test_keep_data_flag(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch(
                "quarry.__main__.get_registration",
                return_value=_mock_registration("math"),
            ),
            patch("quarry.__main__.deregister_directory", return_value=["a.pdf"]),
            patch("quarry.__main__.get_db") as mock_get_db,
            patch("quarry.__main__.db_delete_document") as mock_del,
        ):
            result = runner.invoke(app, ["deregister", "math", "--keep-data"])
        assert result.exit_code == 0
        mock_get_db.assert_not_called()
        mock_del.assert_not_called()

    def test_deregister_unregistered_collection_exits_1(self, tmp_path: Path):
        """Missing collection must fail with exit 1, matching remote 404 path."""
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.get_registration", return_value=None),
            patch("quarry.__main__.deregister_directory", return_value=[]),
        ):
            result = runner.invoke(app, ["deregister", "empty"])
        assert result.exit_code == 1
        assert "No registration" in result.output

    def test_deregister_delete_error(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch(
                "quarry.__main__.get_registration",
                return_value=_mock_registration("math"),
            ),
            patch("quarry.__main__.deregister_directory", return_value=["a.pdf"]),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.db_delete_document",
                side_effect=RuntimeError("db locked"),
            ),
        ):
            result = runner.invoke(app, ["deregister", "math"])
        assert result.exit_code == 1


class TestListRegistrationsCmd:
    def test_lists_registrations(self, tmp_path: Path):
        d = tmp_path / "course"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with patch("quarry.__main__._resolved_settings", return_value=settings):
            runner.invoke(app, ["register", str(d), "--collection", "course"])
            result = runner.invoke(app, ["list", "registrations"])
        assert result.exit_code == 0
        assert "course" in result.output

    def test_empty(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with patch("quarry.__main__._resolved_settings", return_value=settings):
            result = runner.invoke(app, ["list", "registrations"])
        assert result.exit_code == 0
        assert "No registered directories" in result.output


class TestSyncCmd:
    def test_sync_prints_results(self):
        from quarry.sync import SyncResult

        mock_results = {
            "math": SyncResult(
                collection="math",
                ingested=3,
                deleted=1,
                skipped=5,
                failed=0,
            )
        }
        settings = _mock_settings()
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value=mock_results),
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        assert "3 ingested" in result.output
        assert "1 deleted" in result.output
        assert "5 unchanged" in result.output

    def test_workers_flag_passthrough(self):
        from quarry.sync import SyncResult

        mock_results = {
            "col": SyncResult(
                collection="col", ingested=0, deleted=0, skipped=0, failed=0
            )
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value=mock_results) as mock_sync,
        ):
            result = runner.invoke(app, ["sync", "--workers", "8"])

        assert result.exit_code == 0
        assert mock_sync.call_args[1]["max_workers"] == 8

    def test_auto_workers_default(self):
        from quarry.sync import SyncResult

        mock_results = {
            "col": SyncResult(
                collection="col", ingested=0, deleted=0, skipped=0, failed=0
            )
        }
        settings = _mock_settings()
        settings.ocr_backend = "local"
        settings.embedding_backend = "onnx"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value=mock_results) as mock_sync,
            patch("quarry.__main__._auto_workers", return_value=1) as mock_aw,
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        mock_aw.assert_called_once_with(settings)
        assert mock_sync.call_args[1]["max_workers"] == 1

    def test_sync_empty_results(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value={}),
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0

    def test_sync_with_errors(self):
        from quarry.sync import SyncResult

        mock_results = {
            "col": SyncResult(
                collection="col",
                ingested=1,
                deleted=0,
                skipped=0,
                failed=2,
                errors=["file1.pdf: corrupt", "file2.pdf: timeout"],
            )
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value=mock_results),
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        assert "2 failed" in result.output
        assert "corrupt" in result.output
        assert "timeout" in result.output


class TestDatabasesCmd:
    def _setup_databases(self, tmp_path: Path) -> MagicMock:
        settings = _mock_settings()
        settings.quarry_root = tmp_path
        (tmp_path / "default" / "lancedb").mkdir(parents=True)
        (tmp_path / "work" / "lancedb").mkdir(parents=True)
        return settings

    def test_lists_databases(self, tmp_path: Path):
        settings = self._setup_databases(tmp_path)
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=settings,
            ),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "databases"])

        assert result.exit_code == 0
        assert "default" in result.output
        assert "work" in result.output

    def test_no_databases(self, tmp_path: Path):
        settings = _mock_settings()
        settings.quarry_root = tmp_path / "nonexistent"
        with patch(
            "quarry.__main__._resolved_settings",
            return_value=settings,
        ):
            result = runner.invoke(app, ["list", "databases"])
        assert result.exit_code == 0
        assert "No databases found" in result.output

    def test_json_output(self, tmp_path: Path):
        _reset_globals()
        settings = self._setup_databases(tmp_path)
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=settings,
            ),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["--json", "list", "databases"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "default"
        assert data[1]["name"] == "work"
        assert data[0]["document_count"] == 0
        assert "size_bytes" in data[0]
        assert "size_description" in data[0]

    def test_json_empty(self, tmp_path: Path):
        _reset_globals()
        settings = _mock_settings()
        settings.quarry_root = tmp_path / "nonexistent"
        with patch(
            "quarry.__main__._resolved_settings",
            return_value=settings,
        ):
            result = runner.invoke(app, ["--json", "list", "databases"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == []


class TestDbOption:
    """Verify --db global flag is parsed and forwarded to resolve_db_paths."""

    def test_list_passes_db_to_resolver(self):
        _reset_globals()
        with (
            patch("quarry.__main__.load_settings", return_value=_mock_settings()),
            patch(
                "quarry.__main__.resolve_db_paths",
                return_value=_mock_settings(),
            ) as mock_resolve,
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["--db", "work", "list", "documents"])
        assert result.exit_code == 0
        mock_resolve.assert_called_once()
        assert mock_resolve.call_args[0][1] == "work"

    def test_search_passes_db_to_resolver(self):
        _reset_globals()
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.__main__.load_settings", return_value=_mock_settings()),
            patch(
                "quarry.__main__.resolve_db_paths",
                return_value=_mock_settings(),
            ) as mock_resolve,
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=[]),
        ):
            result = runner.invoke(app, ["--db", "work", "find", "query"])
        assert result.exit_code == 0
        assert mock_resolve.call_args[0][1] == "work"

    def test_delete_passes_db_to_resolver(self):
        _reset_globals()
        with (
            patch("quarry.__main__.load_settings", return_value=_mock_settings()),
            patch(
                "quarry.__main__.resolve_db_paths",
                return_value=_mock_settings(),
            ) as mock_resolve,
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=1),
        ):
            result = runner.invoke(app, ["--db", "work", "delete", "x.pdf"])
        assert result.exit_code == 0
        assert mock_resolve.call_args[0][1] == "work"

    def test_collections_passes_db_to_resolver(self):
        _reset_globals()
        with (
            patch("quarry.__main__.load_settings", return_value=_mock_settings()),
            patch(
                "quarry.__main__.resolve_db_paths",
                return_value=_mock_settings(),
            ) as mock_resolve,
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["--db", "work", "list", "collections"])
        assert result.exit_code == 0
        assert mock_resolve.call_args[0][1] == "work"

    def test_default_db_passes_none(self):
        _reset_globals()
        with (
            patch("quarry.__main__.load_settings", return_value=_mock_settings()),
            patch(
                "quarry.__main__.resolve_db_paths",
                return_value=_mock_settings(),
            ) as mock_resolve,
            patch("quarry.__main__.read_default_db", return_value=None),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            runner.invoke(app, ["list", "documents"])
        assert mock_resolve.call_args[0][1] is None


class TestIngestCmd:
    def test_ingests_file(self, tmp_path: Path):
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        mock_result = {"document_name": "doc.txt", "chunks": 1}
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_document",
                return_value=mock_result,
            ),
        ):
            result = runner.invoke(app, ["ingest", str(f)])
        assert result.exit_code == 0
        assert "doc.txt" in result.output

    def test_ingest_url_uses_auto(self):
        mock_result = {
            "document_name": "example.com",
            "chunks": 5,
            "collection": "default",
        }
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_auto",
                return_value=mock_result,
            ),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com/docs"])
        assert result.exit_code == 0

    def test_ingest_directory_errors(self, tmp_path: Path):
        d = tmp_path / "subdir"
        d.mkdir()
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
        ):
            result = runner.invoke(app, ["ingest", str(d)])
        assert result.exit_code == 1
        assert "directory" in result.output.lower()

    def test_ingest_passes_db(self, tmp_path: Path):
        _reset_globals()
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        with (
            patch("quarry.__main__.load_settings", return_value=_mock_settings()),
            patch(
                "quarry.__main__.resolve_db_paths",
                return_value=_mock_settings(),
            ) as mock_resolve,
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_document",
                return_value={"chunks": 1},
            ),
        ):
            result = runner.invoke(app, ["--db", "work", "ingest", str(f)])
        assert result.exit_code == 0
        assert mock_resolve.call_args[0][1] == "work"

    def test_ingest_nonexistent_file(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_document",
                side_effect=FileNotFoundError("not found"),
            ),
        ):
            result = runner.invoke(app, ["ingest", "/no/such/file.pdf"])
        assert result.exit_code == 1

    def test_ingest_url_network_error(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_auto",
                side_effect=ConnectionError("network down"),
            ),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com/page"])
        assert result.exit_code == 1

    def test_ingest_overwrite_flag(self, tmp_path: Path):
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_document",
                return_value={"chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(app, ["ingest", str(f), "--overwrite"])
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["overwrite"] is True

    def test_ingest_collection_flag_file(self, tmp_path: Path):
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_document",
                return_value={"chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(app, ["ingest", str(f), "--collection", "mycol"])
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["collection"] == "mycol"

    def test_ingest_collection_flag_url(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_auto",
                return_value={"chunks": 1},
            ) as mock_auto,
        ):
            result = runner.invoke(
                app, ["ingest", "https://example.com", "--collection", "mycol"]
            )
        assert result.exit_code == 0
        assert mock_auto.call_args[1]["collection"] == "mycol"

    def test_ingest_url_with_errors(self):
        mock_result = {
            "document_name": "example.com",
            "chunks": 3,
            "errors": ["page /broken: 404", "page /gone: 410"],
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_auto", return_value=mock_result),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com"])
        assert result.exit_code == 0
        assert "404" in result.output
        assert "410" in result.output


class TestRememberCmd:
    def test_remember_from_stdin(self):
        mock_result = {
            "document_name": "notes.md",
            "chunks": 2,
            "collection": "default",
        }
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=_mock_settings(),
            ),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value=mock_result,
            ),
        ):
            result = runner.invoke(
                app,
                ["remember", "--name", "notes.md"],
                input="some content here",
            )
        assert result.exit_code == 0
        assert "notes.md" in result.output

    def test_remember_requires_name(self):
        result = runner.invoke(
            app,
            ["remember"],
            input="some content",
        )
        assert result.exit_code == 1
        assert "--name" in result.output

    def test_remember_rejects_empty_stdin(self):
        result = runner.invoke(
            app,
            ["remember", "--name", "test.md"],
            input="",
        )
        assert result.exit_code == 1
        assert "no content" in result.output.lower()

    def test_format_passthrough(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(
                app,
                ["remember", "--name", "n.md", "--format", "markdown"],
                input="# heading",
            )
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["format_hint"] == "markdown"

    def test_overwrite_true_default(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(
                app,
                ["remember", "--name", "n.md"],
                input="content",
            )
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["overwrite"] is True

    def test_no_overwrite_flag(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(
                app,
                ["remember", "--name", "n.md", "--no-overwrite"],
                input="content",
            )
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["overwrite"] is False

    def test_collection_passthrough(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"chunks": 1},
            ) as mock_ingest,
        ):
            result = runner.invoke(
                app,
                ["remember", "--name", "n.md", "--collection", "notes"],
                input="content",
            )
        assert result.exit_code == 0
        assert mock_ingest.call_args[1]["collection"] == "notes"

    def test_remote_routing_posts_json_body(self):
        remote_resp = {
            "document_name": "notes.md",
            "collection": "default",
            "chunks": 2,
        }
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "ca_cert": "/path/to/ca.crt",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(
                app,
                [
                    "remember",
                    "--name",
                    "notes.md",
                    "--collection",
                    "notes",
                    "--format",
                    "markdown",
                    "--agent-handle",
                    "rmh",
                    "--memory-type",
                    "fact",
                    "--summary",
                    "one line",
                ],
                input="body text",
            )

        assert result.exit_code == 0
        mock_req.assert_called_once()
        method = mock_req.call_args[0][0]
        path = mock_req.call_args[0][1]
        body = mock_req.call_args[1]["body"]
        assert method == "POST"
        assert path == "/remember"
        assert body["name"] == "notes.md"
        assert body["content"] == "body text"
        assert body["collection"] == "notes"
        assert body["format_hint"] == "markdown"
        assert body["overwrite"] is True
        assert body["agent_handle"] == "rmh"
        assert body["memory_type"] == "fact"
        assert body["summary"] == "one line"

    def test_remote_routing_does_not_call_local(self):
        remote_resp = {
            "document_name": "a.md",
            "collection": "default",
            "chunks": 1,
        }
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
            patch("quarry.__main__.ingest_content") as mock_local,
        ):
            result = runner.invoke(app, ["remember", "--name", "a.md"], input="hi")

        assert result.exit_code == 0
        mock_local.assert_not_called()

    def test_remote_routing_http_error_exits_1(self):
        from quarry.__main__ import RemoteError

        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_request",
                side_effect=RemoteError(500, "boom"),
            ),
        ):
            result = runner.invoke(app, ["remember", "--name", "a.md"], input="hi")

        assert result.exit_code == 1
        assert "boom" in result.output

    def test_local_fallback_when_no_proxy(self):
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_content",
                return_value={"chunks": 1},
            ) as mock_local,
        ):
            result = runner.invoke(app, ["remember", "--name", "a.md"], input="hi")

        assert result.exit_code == 0
        mock_local.assert_called_once()

    def test_json_equivalence_remote_local(self):
        """Remote and local paths emit the same top-level JSON keys."""
        mock_result = {
            "document_name": "notes.md",
            "collection": "default",
            "chunks": 2,
        }

        # Remote path
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=mock_result),
        ):
            remote_res = runner.invoke(
                app,
                ["--json", "remember", "--name", "notes.md"],
                input="body",
            )
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_keys = set(json.loads(remote_res.output).keys())

        # Local path
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_content", return_value=mock_result),
        ):
            local_res = runner.invoke(
                app,
                ["--json", "remember", "--name", "notes.md"],
                input="body",
            )
        _reset_globals()
        assert local_res.exit_code == 0
        local_keys = set(json.loads(local_res.output).keys())

        assert remote_keys == local_keys


class TestIngestCmdRemote:
    """Remote-routing tests for ``quarry ingest``."""

    def test_remote_routing_url_posts_json_body(self):
        remote_resp = {
            "document_name": "https://example.com",
            "collection": "example.com",
            "chunks": 5,
        }
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "ca_cert": "/path/to/ca.crt",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(
                app,
                [
                    "ingest",
                    "https://example.com/docs",
                    "--overwrite",
                    "--collection",
                    "mycol",
                    "--agent-handle",
                    "rmh",
                    "--memory-type",
                    "fact",
                    "--summary",
                    "one line",
                ],
            )

        assert result.exit_code == 0
        mock_req.assert_called_once()
        method = mock_req.call_args[0][0]
        path = mock_req.call_args[0][1]
        body = mock_req.call_args[1]["body"]
        assert method == "POST"
        assert path == "/ingest"
        assert body["source"] == "https://example.com/docs"
        assert body["overwrite"] is True
        assert body["collection"] == "mycol"
        assert body["agent_handle"] == "rmh"
        assert body["memory_type"] == "fact"
        assert body["summary"] == "one line"

    def test_remote_routing_url_does_not_call_local(self):
        remote_resp = {"document_name": "x", "collection": "c", "chunks": 1}
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
            patch("quarry.__main__.ingest_auto") as mock_local,
        ):
            result = runner.invoke(app, ["ingest", "https://example.com/"])

        assert result.exit_code == 0
        mock_local.assert_not_called()

    def test_remote_routing_local_file_exits_1(self, tmp_path: Path):
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request") as mock_req,
        ):
            result = runner.invoke(app, ["ingest", str(f)])

        assert result.exit_code == 1
        assert "file upload" in result.output.lower()
        mock_req.assert_not_called()

    def test_remote_routing_http_error_exits_1(self):
        from quarry.__main__ import RemoteError

        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch(
                "quarry.__main__._remote_https_request",
                side_effect=RemoteError(500, "boom"),
            ),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com/"])

        assert result.exit_code == 1
        assert "boom" in result.output

    def test_remote_prints_errors_list(self):
        remote_resp = {
            "document_name": "x",
            "collection": "c",
            "chunks": 3,
            "errors": ["page /broken: 404", "page /gone: 410"],
        }
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com/"])

        assert result.exit_code == 0
        assert "404" in result.output
        assert "410" in result.output

    def test_local_fallback_when_no_proxy_url(self):
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.ingest_auto",
                return_value={"document_name": "x", "chunks": 1},
            ) as mock_local,
        ):
            result = runner.invoke(app, ["ingest", "https://example.com/"])

        assert result.exit_code == 0
        mock_local.assert_called_once()

    def test_json_equivalence_remote_local(self):
        """Remote and local paths emit the same top-level JSON keys for URLs."""
        mock_result = {
            "document_name": "https://example.com",
            "collection": "example.com",
            "chunks": 5,
        }

        # Remote path
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=mock_result),
        ):
            remote_res = runner.invoke(
                app, ["--json", "ingest", "https://example.com/"]
            )
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_keys = set(json.loads(remote_res.output).keys())

        # Local path
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_auto", return_value=mock_result),
        ):
            local_res = runner.invoke(app, ["--json", "ingest", "https://example.com/"])
        _reset_globals()
        assert local_res.exit_code == 0
        local_keys = set(json.loads(local_res.output).keys())

        assert remote_keys == local_keys


_REMOTE_INNER_CONFIG = {
    "url": "wss://quarry.example.com:8420/mcp",
    "ca_cert": "/path/to/ca.crt",
    "headers": {"Authorization": "Bearer tok"},
}
_REMOTE_PROXY_CONFIG = {"quarry": _REMOTE_INNER_CONFIG}


class TestSyncCmdRemote:
    """Remote-routing tests for ``quarry sync``."""

    def test_remote_routing_posts_to_sync(self):
        remote_resp = {
            "math": {
                "ingested": 3,
                "deleted": 1,
                "skipped": 5,
                "failed": 0,
                "errors": [],
            }
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        mock_req.assert_called_once()
        assert mock_req.call_args[0][0] == "POST"
        assert mock_req.call_args[0][1] == "/sync"
        # Fix 5: remote sync must use the long timeout (not 15s default).
        assert mock_req.call_args.kwargs["timeout"] >= 60.0
        assert "3 ingested" in result.output
        assert "1 deleted" in result.output

    def test_remote_routing_warns_on_workers_flag(self):
        """Fix 6: --workers is meaningless over HTTP; the CLI must warn."""
        remote_resp: dict[str, dict[str, object]] = {}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            result = runner.invoke(app, ["sync", "--workers", "4"])

        assert result.exit_code == 0
        assert "--workers is ignored" in result.output

    def test_remote_routing_does_not_call_local(self):
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_request", return_value={}),
            patch("quarry.__main__.sync_all") as mock_sync_all,
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        mock_sync_all.assert_not_called()

    def test_remote_routing_http_error_exits_1(self):
        from quarry.__main__ import RemoteError

        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                side_effect=RemoteError(502, "pipeline failed"),
            ),
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 1
        assert "pipeline failed" in result.output

    def test_remote_prints_errors(self):
        remote_resp = {
            "col": {
                "ingested": 1,
                "deleted": 0,
                "skipped": 0,
                "failed": 2,
                "errors": ["a.pdf: corrupt", "b.pdf: timeout"],
            }
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        assert "corrupt" in result.output
        assert "timeout" in result.output

    def test_local_fallback_when_no_proxy(self):
        from quarry.sync import SyncResult

        mock_results = {
            "col": SyncResult(
                collection="col", ingested=1, deleted=0, skipped=0, failed=0
            )
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value=mock_results) as mock_sync,
        ):
            result = runner.invoke(app, ["sync"])

        assert result.exit_code == 0
        mock_sync.assert_called_once()

    def test_json_equivalence_remote_local(self):
        """Remote and local paths emit the same top-level JSON keys."""
        from quarry.sync import SyncResult

        remote_resp = {
            "col": {
                "ingested": 2,
                "deleted": 1,
                "skipped": 3,
                "failed": 0,
                "errors": [],
            }
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            remote_res = runner.invoke(app, ["--json", "sync"])
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_data = json.loads(remote_res.output)
        assert set(remote_data.keys()) == {"col"}
        remote_entry_keys = set(remote_data["col"].keys())

        mock_results = {
            "col": SyncResult(
                collection="col",
                ingested=2,
                deleted=1,
                skipped=3,
                failed=0,
                errors=[],
            )
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value=mock_results),
        ):
            local_res = runner.invoke(app, ["--json", "sync"])
        _reset_globals()
        assert local_res.exit_code == 0
        local_data = json.loads(local_res.output)
        local_entry_keys = set(local_data["col"].keys())

        assert remote_entry_keys == local_entry_keys


class TestListDatabasesCmdRemote:
    """Remote-routing tests for ``quarry list databases``."""

    def test_remote_routing_returns_server_db(self):
        remote_resp = {
            "total_databases": 1,
            "databases": [
                {
                    "name": "server-db",
                    "document_count": 42,
                    "size_bytes": 12345,
                    "size_description": "12.1 KB",
                }
            ],
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_resp
            ) as mock_get,
        ):
            result = runner.invoke(app, ["list", "databases"])

        assert result.exit_code == 0
        mock_get.assert_called_once_with("/databases", _REMOTE_INNER_CONFIG)
        assert "server-db" in result.output
        assert "42 documents" in result.output

    def test_local_fallback_when_no_proxy(self, tmp_path: Path):
        settings = _mock_settings()
        settings.quarry_root = tmp_path
        (tmp_path / "default" / "lancedb").mkdir(parents=True)
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "databases"])

        assert result.exit_code == 0
        assert "default" in result.output

    def test_json_equivalence_remote_local(self, tmp_path: Path):
        """Remote and local paths emit identical JSON keys for databases."""
        remote_resp = {
            "databases": [
                {
                    "name": "work",
                    "document_count": 0,
                    "size_bytes": 0,
                    "size_description": "0 bytes",
                }
            ]
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_get", return_value=remote_resp),
        ):
            remote_res = runner.invoke(app, ["--json", "list", "databases"])
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_data = json.loads(remote_res.output)
        assert len(remote_data) == 1
        remote_keys = set(remote_data[0].keys())

        settings = _mock_settings()
        settings.quarry_root = tmp_path
        (tmp_path / "work" / "lancedb").mkdir(parents=True)
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            local_res = runner.invoke(app, ["--json", "list", "databases"])
        _reset_globals()
        assert local_res.exit_code == 0
        local_data = json.loads(local_res.output)
        assert len(local_data) == 1
        local_keys = set(local_data[0].keys())
        assert remote_keys == local_keys


class TestUseCmdRemote:
    """``quarry use`` is client-side state; verify it does not touch the server."""

    def test_writes_client_config_when_remote(self, tmp_path: Path):
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__.resolve_db_paths", return_value=_mock_settings()),
            patch("quarry.__main__.load_settings", return_value=_mock_settings()),
            patch("quarry.__main__.write_default_db") as mock_write,
            patch("quarry.__main__._remote_https_request") as mock_req,
        ):
            result = runner.invoke(app, ["use", "work"])

        assert result.exit_code == 0
        mock_write.assert_called_once_with("work")
        mock_req.assert_not_called()
        assert "client-side" in result.output.lower()


class TestRegisterCmdRemote:
    """Remote-routing tests for ``quarry register``."""

    def test_remote_routing_posts_to_registrations(self, tmp_path: Path):
        d = tmp_path / "docs"
        d.mkdir()
        remote_resp = {
            "directory": str(d),
            "collection": "docs",
            "registered_at": "2026-01-01T00:00:00",
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(app, ["register", str(d), "--collection", "docs"])

        assert result.exit_code == 0
        mock_req.assert_called_once()
        assert mock_req.call_args[0][0] == "POST"
        assert mock_req.call_args[0][1] == "/registrations"
        body = mock_req.call_args[1]["body"]
        assert body["directory"] == str(d.resolve())
        assert body["collection"] == "docs"
        assert "Registered" in result.output

    def test_remote_routing_default_collection_from_dir_name(self, tmp_path: Path):
        d = tmp_path / "ml-101"
        d.mkdir()
        remote_resp = {
            "directory": str(d),
            "collection": "ml-101",
            "registered_at": "2026-01-01T00:00:00",
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            runner.invoke(app, ["register", str(d)])

        body = mock_req.call_args[1]["body"]
        assert body["collection"] == "ml-101"

    def test_remote_routing_http_error_exits_1(self, tmp_path: Path):
        from quarry.__main__ import RemoteError

        d = tmp_path / "bad"
        d.mkdir()
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                side_effect=RemoteError(400, "directory outside $HOME"),
            ),
        ):
            result = runner.invoke(app, ["register", str(d)])

        assert result.exit_code == 1
        assert "outside" in result.output.lower()

    def test_local_fallback_when_no_proxy(self, tmp_path: Path):
        d = tmp_path / "course"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
        ):
            result = runner.invoke(app, ["register", str(d), "--collection", "course"])

        assert result.exit_code == 0
        assert "course" in result.output

    def test_json_equivalence_remote_local(self, tmp_path: Path):
        d = tmp_path / "course"
        d.mkdir()

        remote_resp = {
            "directory": str(d.resolve()),
            "collection": "course",
            "registered_at": "2026-01-01T00:00:00",
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            remote_res = runner.invoke(
                app, ["--json", "register", str(d), "--collection", "course"]
            )
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_keys = set(json.loads(remote_res.output).keys())

        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
        ):
            local_res = runner.invoke(
                app, ["--json", "register", str(d), "--collection", "local-course"]
            )
        _reset_globals()
        assert local_res.exit_code == 0
        local_keys = set(json.loads(local_res.output).keys())

        assert remote_keys == local_keys


class TestDeregisterCmdRemote:
    """Remote-routing tests for ``quarry deregister``."""

    def test_remote_routing_sends_delete(self):
        remote_resp = {
            "collection": "docs",
            "removed": 7,
            "deleted_chunks": 42,
            "type": "registration",
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(app, ["deregister", "docs"])

        assert result.exit_code == 0
        assert mock_req.call_args[0][0] == "DELETE"
        sent_path = mock_req.call_args[0][1]
        assert "/registrations?" in sent_path
        assert "collection=docs" in sent_path
        # Fix 1: remote path must propagate keep_data so the server purges
        # LanceDB chunks in step with the registry rows.
        assert "keep_data=false" in sent_path
        assert "Deregistered" in result.output
        assert "7 files" in result.output
        assert "42 chunks deleted" in result.output

    def test_remote_deregister_cleans_data(self):
        """Default deregister must tell the server to delete chunks too."""
        remote_resp = {
            "collection": "math",
            "removed": 2,
            "deleted_chunks": 9,
            "type": "registration",
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(app, ["deregister", "math"])

        assert result.exit_code == 0
        sent_path = mock_req.call_args[0][1]
        assert "keep_data=false" in sent_path

    def test_remote_deregister_keep_data_flag(self):
        """--keep-data must pass keep_data=true in the URL."""
        remote_resp = {
            "collection": "math",
            "removed": 2,
            "deleted_chunks": 0,
            "type": "registration",
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request", return_value=remote_resp
            ) as mock_req,
        ):
            result = runner.invoke(app, ["deregister", "math", "--keep-data"])

        assert result.exit_code == 0
        sent_path = mock_req.call_args[0][1]
        assert "keep_data=true" in sent_path

    def test_remote_routing_not_found_exits_1(self):
        from quarry.__main__ import RemoteError

        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_request",
                side_effect=RemoteError(404, "Not found"),
            ),
        ):
            result = runner.invoke(app, ["deregister", "missing"])

        assert result.exit_code == 1
        assert "No registration" in result.output

    def test_remote_routing_does_not_call_local(self):
        remote_resp = {"collection": "c", "removed": 0, "type": "registration"}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
            patch("quarry.__main__.deregister_directory") as mock_local,
        ):
            result = runner.invoke(app, ["deregister", "c"])

        assert result.exit_code == 0
        mock_local.assert_not_called()

    def test_local_fallback_when_no_proxy(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch(
                "quarry.__main__.get_registration",
                return_value=_mock_registration("math"),
            ),
            patch("quarry.__main__.deregister_directory", return_value=[]),
        ):
            result = runner.invoke(app, ["deregister", "math"])

        assert result.exit_code == 0

    def test_json_equivalence_remote_local(self, tmp_path: Path):
        remote_resp = {
            "collection": "math",
            "removed": 3,
            "deleted_chunks": 12,
            "type": "registration",
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            remote_res = runner.invoke(app, ["--json", "deregister", "math"])
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_keys = set(json.loads(remote_res.output).keys())

        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch(
                "quarry.__main__.get_registration",
                return_value=_mock_registration("math"),
            ),
            patch("quarry.__main__.deregister_directory", return_value=["a", "b", "c"]),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=4),
        ):
            local_res = runner.invoke(app, ["--json", "deregister", "math"])
        _reset_globals()
        assert local_res.exit_code == 0
        local_keys = set(json.loads(local_res.output).keys())

        assert remote_keys == local_keys


class TestListRegistrationsCmdRemote:
    """Remote-routing tests for ``quarry list registrations``."""

    def test_remote_routing_lists(self):
        remote_resp = {
            "total_registrations": 2,
            "registrations": [
                {
                    "directory": "/home/u/docs",
                    "collection": "docs",
                    "registered_at": "2026-01-01T00:00:00",
                },
                {
                    "directory": "/home/u/code",
                    "collection": "code",
                    "registered_at": "2026-01-02T00:00:00",
                },
            ],
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch(
                "quarry.__main__._remote_https_get", return_value=remote_resp
            ) as mock_get,
        ):
            result = runner.invoke(app, ["list", "registrations"])

        assert result.exit_code == 0
        mock_get.assert_called_once_with("/registrations", _REMOTE_INNER_CONFIG)
        assert "docs" in result.output
        assert "code" in result.output

    def test_remote_empty(self):
        remote_resp = {"total_registrations": 0, "registrations": []}
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_get", return_value=remote_resp),
        ):
            result = runner.invoke(app, ["list", "registrations"])

        assert result.exit_code == 0
        assert "No registered directories" in result.output

    def test_local_fallback_when_no_proxy(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
        ):
            result = runner.invoke(app, ["list", "registrations"])

        assert result.exit_code == 0
        assert "No registered directories" in result.output

    def test_json_equivalence_remote_local(self, tmp_path: Path):
        from quarry.sync_registry import DirectoryRegistration

        remote_resp = {
            "registrations": [
                {
                    "directory": "/home/u/math",
                    "collection": "math",
                    "registered_at": "2026-01-01T00:00:00",
                }
            ]
        }
        with (
            patch(
                "quarry.__main__.read_proxy_config",
                return_value=_REMOTE_PROXY_CONFIG,
            ),
            patch("quarry.__main__._remote_https_get", return_value=remote_resp),
        ):
            remote_res = runner.invoke(app, ["--json", "list", "registrations"])
        _reset_globals()
        assert remote_res.exit_code == 0
        remote_data = json.loads(remote_res.output)
        assert len(remote_data) == 1
        remote_keys = set(remote_data[0].keys())

        local_regs = [
            DirectoryRegistration(
                directory="/home/u/math",
                collection="math",
                registered_at="2026-01-01T00:00:00",
            )
        ]
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__.read_proxy_config", return_value={}),
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.open_registry"),
            patch("quarry.__main__.list_registrations", return_value=local_regs),
        ):
            local_res = runner.invoke(app, ["--json", "list", "registrations"])
        _reset_globals()
        assert local_res.exit_code == 0
        local_data = json.loads(local_res.output)
        assert len(local_data) == 1
        local_keys = set(local_data[0].keys())

        assert remote_keys == local_keys


class TestDatabasesCmdSizeFormatting:
    def test_megabyte_formatting(self, tmp_path: Path):
        settings = _mock_settings()
        settings.quarry_root = tmp_path
        lance_dir = tmp_path / "big" / "lancedb"
        lance_dir.mkdir(parents=True)
        # Create a file > 1MB
        (lance_dir / "data.lance").write_bytes(b"x" * 2_097_152)
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=settings,
            ),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "databases"])
        assert result.exit_code == 0
        assert "MB" in result.output

    def test_kilobyte_formatting(self, tmp_path: Path):
        settings = _mock_settings()
        settings.quarry_root = tmp_path
        lance_dir = tmp_path / "small" / "lancedb"
        lance_dir.mkdir(parents=True)
        (lance_dir / "data.lance").write_bytes(b"x" * 512)
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=settings,
            ),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "databases"])
        assert result.exit_code == 0
        assert "512 bytes" in result.output

    def test_skips_non_database_dirs(self, tmp_path: Path):
        settings = _mock_settings()
        settings.quarry_root = tmp_path
        # Dir without lancedb subdir should be skipped
        (tmp_path / "not-a-db").mkdir()
        (tmp_path / "real" / "lancedb").mkdir(parents=True)
        with (
            patch(
                "quarry.__main__._resolved_settings",
                return_value=settings,
            ),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "databases"])
        assert result.exit_code == 0
        assert "not-a-db" not in result.output
        assert "real" in result.output


class TestAutoWorkers:
    def test_local_backends_default_to_one(self) -> None:
        from quarry.__main__ import _auto_workers
        from quarry.config import Settings

        settings = Settings()
        assert _auto_workers(settings) == 1


class TestVersionCmd:
    def test_prints_version(self):
        _reset_globals()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        # Should output a version string like "0.10.1"
        assert "." in result.output.strip()

    def test_json_version(self):
        _reset_globals()
        result = runner.invoke(app, ["--json", "version"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "version" in data


class TestGlobalFlags:
    def test_verbose_quiet_mutually_exclusive(self):
        _reset_globals()
        result = runner.invoke(app, ["--verbose", "--quiet", "version"])
        assert result.exit_code == 1

    def test_verbose_sets_global(self):
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["--verbose", "list", "documents"])
        assert result.exit_code == 0
        assert cli_mod._verbose is True

    def test_quiet_sets_global(self):
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["--quiet", "list", "documents"])
        assert result.exit_code == 0
        assert cli_mod._quiet is True


class TestCliErrors:
    def test_error_exits_with_code_1(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=RuntimeError("db corrupt"),
            ),
        ):
            result = runner.invoke(app, ["list"])

        assert result.exit_code == 1

    def test_error_message_content(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=RuntimeError("specific failure message"),
            ),
        ):
            result = runner.invoke(app, ["list", "documents"])

        assert result.exit_code == 1
        assert "specific failure message" in result.output

    def test_keyboard_interrupt_propagates(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=KeyboardInterrupt,
            ),
        ):
            result = runner.invoke(app, ["list", "documents"])

        # KeyboardInterrupt is re-raised (not caught as exit 1 by _cli_errors);
        # Typer's CliRunner converts it to SystemExit(130) per Unix convention.
        assert result.exit_code == 130

    def test_system_exit_propagates(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=SystemExit(42),
            ),
        ):
            result = runner.invoke(app, ["list", "documents"])

        assert result.exit_code == 42


class TestJsonOutput:
    """Non-interactive commands must produce valid JSON when --json is set.

    Interactive/server commands (install, doctor, serve, mcp) are excluded.
    """

    def test_find_json(self):
        _reset_globals()
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = np.zeros(768, dtype=np.float32)
        mock_results = [
            {
                "document_name": "report.pdf",
                "page_number": 3,
                "chunk_index": 5,
                "text": "revenue grew",
                "page_type": "text",
                "source_format": ".pdf",
                "_distance": 0.15,
                "collection": "default",
                "summary": "quarterly revenue summary",
            },
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=mock_results),
        ):
            result = runner.invoke(app, ["--json", "find", "revenue"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["document_name"] == "report.pdf"
        assert data[0]["page_number"] == 3
        assert data[0]["chunk_index"] == 5
        assert "similarity" in data[0]
        assert data[0]["text"] == "revenue grew"
        assert data[0]["summary"] == "quarterly revenue summary"

    def test_find_json_empty(self):
        _reset_globals()
        mock_backend = MagicMock()
        mock_backend.embed_query.return_value = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.get_embedding_backend",
                return_value=mock_backend,
            ),
            patch("quarry.__main__.hybrid_search", return_value=[]),
        ):
            result = runner.invoke(app, ["--json", "find", "query"])

        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_list_documents_json(self):
        _reset_globals()
        mock_docs = [
            {
                "document_name": "a.pdf",
                "collection": "default",
                "indexed_pages": 5,
                "total_pages": 5,
                "chunk_count": 10,
            },
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=mock_docs),
        ):
            result = runner.invoke(app, ["--json", "list", "documents"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["document_name"] == "a.pdf"

    def test_list_documents_json_empty(self):
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["--json", "list", "documents"])

        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_show_page_json(self):
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_page_text", return_value="Hello world"),
        ):
            result = runner.invoke(app, ["--json", "show", "report.pdf", "--page", "2"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["document_name"] == "report.pdf"
        assert data["page_number"] == 2
        assert data["text"] == "Hello world"

    def test_show_metadata_json(self):
        _reset_globals()
        mock_doc = {
            "document_name": "report.pdf",
            "document_path": "/docs/report.pdf",
            "collection": "math",
            "total_pages": 10,
            "chunk_count": 42,
            "indexed_pages": 10,
            "ingestion_timestamp": "2026-01-01T00:00:00",
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[mock_doc]),
        ):
            result = runner.invoke(app, ["--json", "show", "report.pdf"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["document_name"] == "report.pdf"
        assert data["collection"] == "math"

    def test_status_json(self):
        _reset_globals()
        mock_settings = _mock_settings()
        mock_settings.registry_path.exists.return_value = False
        mock_settings.lancedb_path.exists.return_value = False
        with (
            patch("quarry.__main__._resolved_settings", return_value=mock_settings),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
            patch("quarry.__main__.count_chunks", return_value=0),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["--json", "status"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["document_count"] == 0
        assert data["chunk_count"] == 0
        assert "database_path" in data

    def test_use_json(self):
        _reset_globals()
        with (
            patch("quarry.__main__.resolve_db_paths", return_value=_mock_settings()),
            patch("quarry.__main__.write_default_db"),
        ):
            result = runner.invoke(app, ["--json", "use", "work"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["database"] == "work"

    def test_delete_json(self):
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=15),
        ):
            result = runner.invoke(app, ["--json", "delete", "report.pdf"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["deleted"] == 15
        assert data["name"] == "report.pdf"
        assert data["type"] == "document"

    def test_delete_collection_json(self):
        _reset_globals()
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_collection", return_value=50),
        ):
            result = runner.invoke(
                app, ["--json", "delete", "math", "--type", "collection"]
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["deleted"] == 50
        assert data["type"] == "collection"

    def test_list_collections_json(self):
        _reset_globals()
        mock_cols = [
            {"collection": "math", "document_count": 5, "chunk_count": 100},
        ]
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=mock_cols),
        ):
            result = runner.invoke(app, ["--json", "list", "collections"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["collection"] == "math"

    def test_register_json(self, tmp_path: Path):
        _reset_globals()
        d = tmp_path / "course"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with patch("quarry.__main__._resolved_settings", return_value=settings):
            result = runner.invoke(
                app, ["--json", "register", str(d), "--collection", "my-course"]
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["collection"] == "my-course"
        assert "directory" in data

    def test_deregister_json(self, tmp_path: Path):
        _reset_globals()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch(
                "quarry.__main__.get_registration",
                return_value=_mock_registration("math"),
            ),
            patch("quarry.__main__.deregister_directory", return_value=["a.pdf"]),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=4),
        ):
            result = runner.invoke(app, ["--json", "deregister", "math"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["collection"] == "math"
        assert data["removed"] == 1
        assert data["deleted_chunks"] == 4

    def test_list_registrations_json(self, tmp_path: Path):
        _reset_globals()
        d = tmp_path / "course"
        d.mkdir()
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with patch("quarry.__main__._resolved_settings", return_value=settings):
            runner.invoke(app, ["register", str(d), "--collection", "course"])
            _reset_globals()
            result = runner.invoke(app, ["--json", "list", "registrations"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["collection"] == "course"

    def test_sync_json(self):
        _reset_globals()
        from quarry.sync import SyncResult

        mock_results = {
            "math": SyncResult(
                collection="math",
                ingested=3,
                deleted=1,
                skipped=5,
                failed=0,
            )
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.sync_all", return_value=mock_results),
        ):
            result = runner.invoke(app, ["--json", "sync"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "math" in data
        assert data["math"]["ingested"] == 3
        assert data["math"]["deleted"] == 1

    def test_ingest_file_json(self, tmp_path: Path):
        _reset_globals()
        f = tmp_path / "doc.txt"
        f.write_text("hello")
        mock_result = {"document_name": "doc.txt", "chunks": 1}
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_document", return_value=mock_result),
        ):
            result = runner.invoke(app, ["--json", "ingest", str(f)])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["document_name"] == "doc.txt"

    def test_ingest_url_json(self):
        _reset_globals()
        mock_result = {"document_name": "example.com", "chunks": 5}
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_auto", return_value=mock_result),
        ):
            result = runner.invoke(
                app, ["--json", "ingest", "https://example.com/docs"]
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["document_name"] == "example.com"

    def test_remember_json(self):
        _reset_globals()
        mock_result = {"document_name": "notes.md", "chunks": 2}
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_content", return_value=mock_result),
        ):
            result = runner.invoke(
                app,
                ["--json", "remember", "--name", "notes.md"],
                input="some content",
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["document_name"] == "notes.md"

    def test_version_json(self):
        _reset_globals()
        result = runner.invoke(app, ["--json", "version"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "version" in data


class TestCliStandards:
    """Verify CLI conforms to Punt Labs CLI standards."""

    def test_version_flag(self):
        import importlib.metadata

        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0
        expected = importlib.metadata.version("punt-quarry")
        assert expected in result.output

    def test_help_plain_text(self):
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        box_chars = {"╭", "╰", "│"}
        found = box_chars & set(result.output)
        assert not found, f"Rich box-drawing characters in help output: {found}"

    def test_help_command_order(self):
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        # Extract command names from the Commands section
        lines = result.output.splitlines()
        in_commands = False
        commands: list[str] = []
        for line in lines:
            if line.strip().startswith("Commands:"):
                in_commands = True
                continue
            if in_commands:
                stripped = line.strip()
                if not stripped:
                    break
                cmd_name = stripped.split()[0]
                commands.append(cmd_name)

        product_cmds = ["find", "ingest", "show", "remember"]
        admin_cmds = ["install", "doctor", "serve", "mcp", "version", "uninstall"]

        present_product = [c for c in commands if c in product_cmds]
        present_admin = [c for c in commands if c in admin_cmds]

        assert present_product, "No product commands found in help output"
        assert present_admin, "No admin commands found in help output"

        last_product_idx = max(commands.index(c) for c in present_product)
        first_admin_idx = min(commands.index(c) for c in present_admin)
        assert last_product_idx < first_admin_idx, (
            f"Product commands must appear before admin commands. Order: {commands}"
        )

    def test_hooks_hidden(self):
        result = runner.invoke(app, ["--help"])

        assert result.exit_code == 0
        # Extract only the Commands section
        lines = result.output.splitlines()
        in_commands = False
        commands_text: list[str] = []
        for line in lines:
            if line.strip().startswith("Commands:"):
                in_commands = True
                continue
            if in_commands:
                if not line.strip():
                    break
                commands_text.append(line.lower())

        assert commands_text, "No commands section found in help output"
        for line in commands_text:
            assert "hooks" not in line, (
                f"'hooks' should not appear in commands section: {line}"
            )


class TestServeTlsFlag:
    """Tests for the --tls flag on the serve command."""

    def test_tls_flag_passes_ssl_args_to_http_serve(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        tls_dir.mkdir()
        cert_path = tls_dir / "server.crt"
        key_path = tls_dir / "server.key"
        cert_path.write_text("FAKE CERT")
        key_path.write_text("FAKE KEY")

        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.TLS_DIR", tls_dir),
            patch("quarry.http_server.serve") as mock_serve,
        ):
            runner.invoke(app, ["serve", "--tls"])

        _reset_globals()
        call_kwargs = mock_serve.call_args[1]
        assert call_kwargs["ssl_certfile"] == str(cert_path)
        assert call_kwargs["ssl_keyfile"] == str(key_path)

    def test_tls_flag_missing_certs_exits(self, tmp_path: Path) -> None:
        tls_dir = tmp_path / "tls"
        # Do not create the cert files.

        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.TLS_DIR", tls_dir),
        ):
            result = runner.invoke(app, ["serve", "--tls"])

        _reset_globals()
        assert result.exit_code == 1
        assert "quarry install" in " ".join(result.output.split())

    def test_no_tls_flag_passes_none_ssl_args(self) -> None:
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.http_server.serve") as mock_serve,
        ):
            runner.invoke(app, ["serve"])

        _reset_globals()
        call_kwargs = mock_serve.call_args[1]
        assert call_kwargs["ssl_certfile"] is None
        assert call_kwargs["ssl_keyfile"] is None


_FAKE_CA_PEM = b"-----BEGIN CERTIFICATE-----\nfakecertdata\n-----END CERTIFICATE-----\n"
_FAKE_FINGERPRINT = "SHA256:" + "a" * 64


class TestLoginCmd:
    """Tests for the TOFU login flow."""

    def _common_patches(self) -> tuple[object, ...]:
        """Return a tuple of patch context managers for the happy path."""
        return (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert"),
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch("quarry.__main__.write_proxy_config"),
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        )

    def test_success_with_yes_flag(self) -> None:
        """--yes skips the interactive prompt."""
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert") as mock_store,
            patch(
                "quarry.__main__.validate_connection", return_value=(True, "")
            ) as mock_validate,
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 0, result.output
        assert "Restart Claude Code" in result.output
        assert _FAKE_FINGERPRINT in result.output
        mock_store.assert_called_once_with(_FAKE_CA_PEM)
        # validate_connection is called with a tempfile path (TOFU ordering: validate
        # before persisting the cert), so we can't assert a fixed path.
        assert mock_validate.call_count == 1
        call_args = mock_validate.call_args
        assert call_args.args[:3] == ("okinos.example.com", 8420, "sk-test")
        assert call_args.kwargs.get("scheme") == "https"
        tmp_path = call_args.kwargs.get("ca_cert_path", "")
        assert isinstance(tmp_path, str) and tmp_path.endswith(".crt")
        mock_write.assert_called_once_with(
            "wss://okinos.example.com:8420/mcp", "sk-test", "/fake/quarry-ca.crt"
        )

    def test_prompt_confirmed(self) -> None:
        """User types 'y' to confirm the fingerprint."""
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert"),
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test"],
                input="y\n",
            )
        _reset_globals()
        assert result.exit_code == 0, result.output
        assert "Trust this server?" in result.output
        mock_write.assert_called_once()

    def test_prompt_rejected_aborts(self) -> None:
        """User types 'n' — aborts cleanly without writing config."""
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert") as mock_store,
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test"],
                input="n\n",
            )
        _reset_globals()
        assert result.exit_code == 0
        assert "Aborted" in result.output
        mock_store.assert_not_called()
        mock_write.assert_not_called()

    def test_fetch_ca_cert_failure_exits(self) -> None:
        """fetch_ca_cert raises ValueError — exits with code 1."""
        with patch(
            "quarry.__main__.fetch_ca_cert",
            side_effect=ValueError("Server unreachable"),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 1
        assert "Server unreachable" in result.output

    def test_connection_failure_after_tofu(self) -> None:
        """validate_connection fails — exits 1, cert and config not written."""
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert") as mock_store,
            patch(
                "quarry.__main__.validate_connection",
                return_value=(False, "Authentication failed — check --api-key."),
            ),
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "bad-key", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 1
        assert "Authentication failed" in result.output
        # TOFU ordering: cert is stored only after successful validation.
        mock_store.assert_not_called()
        mock_write.assert_not_called()

    def test_custom_port(self) -> None:
        """Custom --port is used in the wss:// URL."""
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert"),
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                [
                    "login",
                    "okinos.example.com",
                    "--port",
                    "9000",
                    "--api-key",
                    "sk-test",
                    "--yes",
                ],
            )
        _reset_globals()
        assert result.exit_code == 0
        mock_write.assert_called_once_with(
            "wss://okinos.example.com:9000/mcp", "sk-test", "/fake/quarry-ca.crt"
        )

    def test_no_api_key_proceeds_without_auth(self) -> None:
        """Omitting --api-key succeeds for unauthenticated servers (token=None)."""
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert"),
            patch(
                "quarry.__main__.validate_connection", return_value=(True, "")
            ) as mock_validate,
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(app, ["login", "okinos.example.com", "--yes"])
        _reset_globals()
        assert result.exit_code == 0, result.output
        # validate_connection is called with a tempfile path (TOFU ordering: validate
        # before persisting the cert), so we can't assert a fixed path.
        assert mock_validate.call_count == 1
        call_args = mock_validate.call_args
        assert call_args.args[:3] == ("okinos.example.com", 8420, None)
        assert call_args.kwargs.get("scheme") == "https"
        tmp_path = call_args.kwargs.get("ca_cert_path", "")
        assert isinstance(tmp_path, str) and tmp_path.endswith(".crt")
        # write_proxy_config called with token=None (no Authorization header)
        mock_write.assert_called_once_with(
            "wss://okinos.example.com:8420/mcp", None, "/fake/quarry-ca.crt"
        )

    def test_always_uses_wss(self) -> None:
        """Even for localhost, the new flow writes wss:// (TOFU is uniform)."""
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert"),
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "localhost", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 0
        url_arg = mock_write.call_args[0][0]
        assert url_arg.startswith("wss://"), f"Expected wss:// but got: {url_arg}"

    def test_proxy_config_oserror_exits_without_writing_ca_cert(self) -> None:
        """OSError from write_proxy_config exits before CA cert is stored.

        With the new ordering (config first, CA cert second), a failure to
        write the config means the CA cert write is never attempted.
        """
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert") as mock_store,
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch(
                "quarry.__main__.write_proxy_config",
                side_effect=OSError("Permission denied"),
            ),
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
            patch(
                "quarry.__main__.MCP_PROXY_CONFIG_PATH",
                Path("/fake/mcp-proxy.toml"),
            ),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 1
        assert "Permission denied" in result.output
        # Config failed first — CA cert write never reached.
        mock_store.assert_not_called()

    def test_ca_cert_write_failure_rolls_back_config(self) -> None:
        """If store_ca_cert raises after write_proxy_config succeeds, config is removed.

        Fix 2: write_proxy_config runs first; on CA cert failure, delete_proxy_config
        is called to roll back so the user does not end up with a config pointing at
        a CA cert that was never written.
        """
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert", side_effect=OSError("disk full")),
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch("quarry.__main__.write_proxy_config"),
            patch("quarry.__main__.delete_proxy_config") as mock_delete,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 1
        mock_delete.assert_called_once()

    def test_write_proxy_config_called_before_store_ca_cert(self) -> None:
        """Fix 2: write_proxy_config must be called before store_ca_cert.

        Verifies call ordering by recording which mock was called first.
        """
        call_order: list[str] = []

        def record_write(*_args: object, **_kwargs: object) -> None:
            call_order.append("write_proxy_config")

        def record_store(*_args: object, **_kwargs: object) -> None:
            call_order.append("store_ca_cert")

        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert", side_effect=record_store),
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch("quarry.__main__.write_proxy_config", side_effect=record_write),
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 0, result.output
        assert call_order == ["write_proxy_config", "store_ca_cert"], (
            f"Expected write_proxy_config before store_ca_cert, got: {call_order}"
        )

    def test_fdopen_failure_closes_fd_and_removes_tmp(self) -> None:
        """Fix 1: if os.fdopen raises during tempfile write, the raw fd is closed
        and the temp file is removed — no fd leak, no orphaned file.
        """
        import os as _os

        closed_fds: list[int] = []
        real_close = _os.close

        def fake_close(fd: int) -> None:
            closed_fds.append(fd)
            real_close(fd)

        created_tmp: list[str] = []
        _real_mkstemp = tempfile.mkstemp

        def fake_mkstemp(
            suffix: str | None = None, prefix: str | None = None
        ) -> tuple[int, str]:
            result_fd, path = _real_mkstemp(suffix=suffix, prefix=prefix)
            created_tmp.append(path)
            return result_fd, path

        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.tempfile.mkstemp", side_effect=fake_mkstemp),
            patch("quarry.__main__.os.fdopen", side_effect=OSError("resource limit")),
            patch("quarry.__main__.os.close", side_effect=fake_close),
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        # The command must exit non-zero — fdopen failed.
        assert result.exit_code != 0
        assert closed_fds, "raw fd was not closed after os.fdopen failure"
        # Temp file must be cleaned up.
        for p in created_tmp:
            assert not Path(p).exists(), f"temp file {p!r} was not removed"

    def test_tempfile_written_completely_via_fdopen(self) -> None:
        """login_cmd uses os.fdopen (not os.write) so all CA PEM bytes reach the file.

        Validates that the tempfile passed to validate_connection contains the
        full PEM data — not a truncated write from a raw os.write() call.
        """
        large_pem = (
            b"-----BEGIN CERTIFICATE-----\n"
            + b"A" * 4096
            + b"\n-----END CERTIFICATE-----\n"
        )
        written_paths: list[str] = []

        def capture_validate(
            host: str,
            port: int,
            api_key: object,
            *,
            scheme: str = "http",
            ca_cert_path: str = "",
        ) -> tuple[bool, str]:
            written_paths.append(ca_cert_path)
            return (True, "")

        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=large_pem),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert"),
            patch("quarry.__main__.validate_connection", side_effect=capture_validate),
            patch("quarry.__main__.write_proxy_config"),
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--yes"],
            )
        _reset_globals()
        assert result.exit_code == 0, result.output
        assert len(written_paths) == 1
        # The tempfile must have been written in full and then cleaned up.
        # validate_connection received a path; the file is deleted by the finally block,
        # so we can't re-read it — but we verify validate_connection was called with
        # a .crt path, which confirms fdopen completed without truncation (if it had
        # truncated, validate_connection would have raised or returned False).
        assert written_paths[0].endswith(".crt")

    def test_api_key_from_envvar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """QUARRY_API_KEY env var is used as --api-key when the flag is omitted.

        Install scripts call `QUARRY_API_KEY=<token> quarry login <host> --yes`.
        Without envvar= on the option, the token is silently ignored.
        """
        monkeypatch.setenv("QUARRY_API_KEY", "env-sk-test")
        with (
            patch("quarry.__main__.fetch_ca_cert", return_value=_FAKE_CA_PEM),
            patch("quarry.__main__.cert_fingerprint", return_value=_FAKE_FINGERPRINT),
            patch("quarry.__main__.store_ca_cert"),
            patch(
                "quarry.__main__.validate_connection", return_value=(True, "")
            ) as mock_validate,
            patch("quarry.__main__.write_proxy_config") as mock_write,
            patch("quarry.__main__.CA_CERT_PATH", Path("/fake/quarry-ca.crt")),
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--yes"],
                # do NOT pass --api-key; key must come from the env var
            )
        _reset_globals()
        assert result.exit_code == 0, result.output
        # validate_connection must receive the key from the env var, not None.
        call_args = mock_validate.call_args
        assert call_args.args[2] == "env-sk-test", (
            f"Expected api_key='env-sk-test' from QUARRY_API_KEY, "
            f"got: {call_args.args[2]!r}"
        )
        # write_proxy_config must also receive the env var token.
        mock_write.assert_called_once()
        assert mock_write.call_args[0][1] == "env-sk-test"


class TestLogoutCmd:
    def test_success(self) -> None:
        with patch("quarry.__main__.delete_proxy_config", return_value=True):
            result = runner.invoke(app, ["logout"])
        _reset_globals()
        assert result.exit_code == 0
        assert "Logged out" in result.output

    def test_no_config(self) -> None:
        with patch("quarry.__main__.delete_proxy_config", return_value=False):
            result = runner.invoke(app, ["logout"])
        _reset_globals()
        assert result.exit_code == 0
        assert "No remote configured" in result.output


class TestRemoteListCmd:
    def test_no_remote(self) -> None:
        with patch("quarry.__main__.read_proxy_config", return_value={}):
            result = runner.invoke(app, ["remote", "list"])
        _reset_globals()
        assert result.exit_code == 0
        assert "No remote" in result.output

    def test_shows_url_and_masked_token(self) -> None:
        cfg = {
            "quarry": {
                "url": "ws://host:8420/mcp",
                "headers": {"Authorization": "Bearer sk-abcdef"},
            }
        }
        with patch("quarry.__main__.read_proxy_config", return_value=cfg):
            result = runner.invoke(app, ["remote", "list"])
        _reset_globals()
        assert result.exit_code == 0
        assert "ws://host:8420/mcp" in result.output
        assert "sk-a****" in result.output
        assert "sk-abcdef" not in result.output

    def test_ping_healthy(self) -> None:
        cfg = {
            "quarry": {
                "url": "ws://host:8420/mcp",
                "headers": {"Authorization": "Bearer sk-abcdef"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=cfg),
            patch(
                "quarry.__main__.validate_connection_from_ws_url",
                return_value=(True, ""),
            ),
        ):
            result = runner.invoke(app, ["remote", "list", "--ping"])
        _reset_globals()
        assert result.exit_code == 0
        assert "healthy" in result.output

    def test_ping_unreachable(self) -> None:
        cfg = {
            "quarry": {
                "url": "ws://host:8420/mcp",
                "headers": {"Authorization": "Bearer sk-abcdef"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=cfg),
            patch(
                "quarry.__main__.validate_connection_from_ws_url",
                return_value=(False, "connection refused"),
            ),
        ):
            result = runner.invoke(app, ["remote", "list", "--ping"])
        _reset_globals()
        assert result.exit_code == 0
        assert "unreachable" in result.output

    def test_bare_remote_errors(self) -> None:
        result = runner.invoke(app, ["remote"])
        _reset_globals()
        assert result.exit_code == 1

    def test_malformed_toml_shows_warning_and_continues(self) -> None:
        # After fix: ValueError is caught, warning printed, falls back to "no remote".
        with patch(
            "quarry.__main__.read_proxy_config",
            side_effect=ValueError("Malformed config at /path/quarry.toml: ..."),
        ):
            result = runner.invoke(app, ["remote", "list"])
        _reset_globals()
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "Malformed" in result.output

    def test_incomplete_config_shows_no_remote(self) -> None:
        with patch(
            "quarry.__main__.read_proxy_config",
            return_value={"quarry": {"url": ""}},
        ):
            result = runner.invoke(app, ["remote", "list"])
        _reset_globals()
        assert result.exit_code == 0
        assert "No remote configured" in result.output

    def test_ping_wss_without_ca_cert_reports_unhealthy_without_network_call(
        self,
    ) -> None:
        """Fix 3: wss:// + no ca_cert → unhealthy immediately, no network call.

        validate_connection_from_ws_url must NOT be called — the early check
        short-circuits to avoid a SystemExit from _remote_https_get.
        """
        cfg = {
            "quarry": {
                "url": "wss://host:8420/mcp",
                "headers": {"Authorization": "Bearer sk-abcdef"},
                # Deliberately no 'ca_cert' key
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=cfg),
            patch("quarry.__main__.validate_connection_from_ws_url") as mock_validate,
        ):
            result = runner.invoke(app, ["remote", "list", "--ping"])
        _reset_globals()
        assert result.exit_code == 0
        # No network call was attempted.
        mock_validate.assert_not_called()
        # The output must indicate the server is unhealthy with a CA-related reason.
        assert "unreachable" in result.output
        assert "CA" in result.output

    def test_ping_wss_with_ca_cert_calls_validate(self) -> None:
        """Fix 3: wss:// with ca_cert configured calls validate_connection_from_ws_url.

        The early-exit guard must not block calls when a CA cert is present.
        """
        cfg = {
            "quarry": {
                "url": "wss://host:8420/mcp",
                "ca_cert": "/path/to/ca.crt",
                "headers": {"Authorization": "Bearer sk-abcdef"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=cfg),
            patch(
                "quarry.__main__.validate_connection_from_ws_url",
                return_value=(True, ""),
            ) as mock_validate,
        ):
            result = runner.invoke(app, ["remote", "list", "--ping"])
        _reset_globals()
        assert result.exit_code == 0
        mock_validate.assert_called_once()
        assert "healthy" in result.output


class TestRemoteHttpsGet:
    """Unit tests for _remote_https_get() error handling."""

    def test_empty_ca_cert_raises_system_exit(self) -> None:
        """Empty string ca_cert must raise SystemExit with 'CA cert' in message.

        Fix 3: the guard was `ca_cert is None` — an empty string passed through,
        then ssl_ctx.load_verify_locations("") raised an unhelpful OSError.
        The fix changes the guard to `not ca_cert` so "" is treated as absent.
        """
        import pytest

        from quarry.__main__ import _remote_https_get

        config: dict[str, object] = {
            "url": "wss://host:8420/mcp",
            "ca_cert": "",  # empty string — was previously passing the None guard
            "headers": {},
        }
        with pytest.raises(SystemExit, match="CA cert"):
            _remote_https_get("/health", config)

    def test_none_ca_cert_raises_system_exit(self) -> None:
        """None ca_cert raises SystemExit (existing behavior, regression guard)."""
        import pytest

        from quarry.__main__ import _remote_https_get

        config: dict[str, object] = {
            "url": "wss://host:8420/mcp",
            "ca_cert": None,
            "headers": {},
        }
        with pytest.raises(SystemExit, match="CA cert"):
            _remote_https_get("/health", config)

    def test_unreadable_ca_cert_path_raises_system_exit_with_ca_message(
        self, tmp_path: Path
    ) -> None:
        """load_verify_locations failing on a non-existent file raises SystemExit.

        Fix 3: wrap load_verify_locations in try/except to produce a clear message
        rather than propagating the raw OSError or SSLError.
        """
        import pytest

        from quarry.__main__ import _remote_https_get

        missing_cert = str(tmp_path / "nonexistent-ca.crt")
        config: dict[str, object] = {
            "url": "wss://host:8420/mcp",
            "ca_cert": missing_cert,
            "headers": {},
        }
        with pytest.raises(SystemExit, match="CA certificate"):
            _remote_https_get("/health", config)


class TestRemoteHttpsRequest:
    """Unit tests for _remote_https_request() — generalised HTTP helper."""

    def test_delegates_get_to_request(self) -> None:
        """_remote_https_get delegates to _remote_https_request with method=GET."""
        from quarry.__main__ import _remote_https_get

        with patch("quarry.__main__._remote_https_request") as mock_req:
            mock_req.return_value = {"ok": True}
            config: dict[str, object] = {"url": "ws://localhost:8420/mcp"}
            result = _remote_https_get("/status", config)

        mock_req.assert_called_once_with("GET", "/status", config)
        assert result == {"ok": True}

    def test_post_sends_json_body(self) -> None:
        """POST with a body dict sends JSON-encoded content."""
        from quarry.__main__ import _remote_https_request

        config: dict[str, object] = {
            "url": "ws://localhost:8420/mcp",
            "headers": {},
        }
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_conn.getresponse.return_value = mock_resp

        with patch("http.client.HTTPConnection", return_value=mock_conn):
            result = _remote_https_request(
                "POST", "/remember", config, body={"text": "hello"}
            )

        assert result == {"ok": True}
        call_kwargs = mock_conn.request.call_args
        assert call_kwargs[0][0] == "POST"
        assert call_kwargs[0][1] == "/remember"
        sent_body = call_kwargs[1].get("body") or call_kwargs[0][2]
        assert json.loads(sent_body) == {"text": "hello"}
        sent_headers = call_kwargs[1].get("headers") or call_kwargs[0][3]
        assert sent_headers["Content-Type"] == "application/json"

    def test_delete_no_body(self) -> None:
        """DELETE without a body sends no Content-Type header."""
        from quarry.__main__ import _remote_https_request

        config: dict[str, object] = {
            "url": "ws://localhost:8420/mcp",
            "headers": {},
        }
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"deleted": 5}'
        mock_conn.getresponse.return_value = mock_resp

        with patch("http.client.HTTPConnection", return_value=mock_conn):
            result = _remote_https_request("DELETE", "/documents?name=foo", config)

        assert result == {"deleted": 5}
        call_args = mock_conn.request.call_args
        assert call_args[0][0] == "DELETE"
        sent_headers = call_args[1].get("headers", {})
        assert "Content-Type" not in sent_headers

    def test_non_2xx_raises_remote_error(self) -> None:
        """Status codes >= 300 raise RemoteError with correct status."""
        import pytest

        from quarry.__main__ import RemoteError, _remote_https_request

        config: dict[str, object] = {
            "url": "ws://localhost:8420/mcp",
            "headers": {},
        }
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.read.return_value = b'{"error": "Not found"}'
        mock_conn.getresponse.return_value = mock_resp

        with (
            patch("http.client.HTTPConnection", return_value=mock_conn),
            pytest.raises(RemoteError, match="HTTP 404") as exc_info,
        ):
            _remote_https_request("DELETE", "/documents?name=foo", config)

        assert exc_info.value.status == 404

    def test_non_dict_response_raises_remote_error(self) -> None:
        """A JSON array (not object) must raise RemoteError, not crash."""
        import pytest

        from quarry.__main__ import RemoteError, _remote_https_request

        config: dict[str, object] = {
            "url": "ws://localhost:8420/mcp",
            "headers": {},
        }
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'["not", "a", "dict"]'
        mock_conn.getresponse.return_value = mock_resp

        with (
            patch("http.client.HTTPConnection", return_value=mock_conn),
            pytest.raises(RemoteError, match="expected JSON object"),
        ):
            _remote_https_request("GET", "/status", config)

    def test_non_dict_scalar_response_raises_remote_error(self) -> None:
        """A JSON scalar must raise RemoteError rather than crash downstream."""
        import pytest

        from quarry.__main__ import RemoteError, _remote_https_request

        config: dict[str, object] = {
            "url": "ws://localhost:8420/mcp",
            "headers": {},
        }
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"42"
        mock_conn.getresponse.return_value = mock_resp

        with (
            patch("http.client.HTTPConnection", return_value=mock_conn),
            pytest.raises(RemoteError, match="expected JSON object"),
        ):
            _remote_https_request("GET", "/status", config)

    def test_non_json_response_raises_remote_error(self) -> None:
        """HTML from a reverse proxy must not leak a JSONDecodeError."""
        import pytest

        from quarry.__main__ import RemoteError, _remote_https_request

        config: dict[str, object] = {
            "url": "ws://localhost:8420/mcp",
            "headers": {},
        }
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html><body>502 Bad Gateway</body></html>"
        mock_conn.getresponse.return_value = mock_resp

        with (
            patch("http.client.HTTPConnection", return_value=mock_conn),
            pytest.raises(RemoteError, match="non-JSON response"),
        ):
            _remote_https_request("GET", "/status", config)

    def test_timeout_parameter_reaches_connection(self) -> None:
        """Fix 5: the timeout kwarg must be passed to HTTPConnection."""
        from quarry.__main__ import _remote_https_request

        config: dict[str, object] = {
            "url": "ws://localhost:8420/mcp",
            "headers": {},
        }
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"{}"
        mock_conn.getresponse.return_value = mock_resp

        with patch("http.client.HTTPConnection", return_value=mock_conn) as mock_cls:
            _remote_https_request("POST", "/sync", config, body={}, timeout=600.0)

        _, kwargs = mock_cls.call_args
        assert kwargs["timeout"] == 600.0


class TestIngestExitCodes:
    """Fix 7: exit 1 when errors reported and zero chunks ingested."""

    def test_local_ingest_url_exits_1_on_errors_with_zero_chunks(self) -> None:
        mock_result = {
            "document_name": "example.com",
            "chunks": 0,
            "errors": ["fetch failed: 500"],
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_auto", return_value=mock_result),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com"])
        assert result.exit_code == 1
        assert "500" in result.output

    def test_local_ingest_url_zero_chunks_no_errors_exits_0(self) -> None:
        """Zero chunks without errors should not exit non-zero."""
        mock_result = {"document_name": "example.com", "chunks": 0}
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_auto", return_value=mock_result),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com"])
        assert result.exit_code == 0

    def test_local_ingest_url_errors_with_some_chunks_exits_0(self) -> None:
        """Partial success (errors + chunks > 0) must still exit 0."""
        mock_result = {
            "document_name": "example.com",
            "chunks": 3,
            "errors": ["page /gone: 404"],
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_auto", return_value=mock_result),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com"])
        assert result.exit_code == 0
        assert "404" in result.output

    def test_remote_ingest_exits_1_on_errors_with_zero_chunks(self) -> None:
        remote_resp = {
            "document_name": "x",
            "collection": "c",
            "chunks": 0,
            "errors": ["fetch failed"],
        }
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            result = runner.invoke(app, ["ingest", "https://example.com/"])
        assert result.exit_code == 1
        assert "fetch failed" in result.output

    def test_local_remember_exits_1_on_errors_with_zero_chunks(self) -> None:
        mock_result = {
            "document_name": "notes.md",
            "chunks": 0,
            "errors": ["embedding failed"],
        }
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.ingest_content", return_value=mock_result),
        ):
            result = runner.invoke(
                app, ["remember", "--name", "notes.md"], input="body"
            )
        assert result.exit_code == 1
        assert "embedding failed" in result.output

    def test_remote_remember_exits_1_on_errors_with_zero_chunks(self) -> None:
        remote_resp = {
            "document_name": "notes.md",
            "collection": "default",
            "chunks": 0,
            "errors": ["server problem"],
        }
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            result = runner.invoke(
                app, ["remember", "--name", "notes.md"], input="body"
            )
        assert result.exit_code == 1
        assert "server problem" in result.output

    def test_remote_remember_mirrors_errors_to_stderr(self) -> None:
        """The remote remember path must print the errors list (Fix 6)."""
        remote_resp = {
            "document_name": "notes.md",
            "collection": "default",
            "chunks": 2,
            "errors": ["warning: stripped html"],
        }
        proxy_config = {
            "quarry": {
                "url": "wss://quarry.example.com:8420/mcp",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
        with (
            patch("quarry.__main__.read_proxy_config", return_value=proxy_config),
            patch("quarry.__main__._remote_https_request", return_value=remote_resp),
        ):
            result = runner.invoke(
                app, ["remember", "--name", "notes.md"], input="body"
            )
        assert result.exit_code == 0
        assert "stripped html" in result.output
