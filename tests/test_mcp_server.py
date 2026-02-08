from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from quarry.mcp_server import delete_document, status


def _settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    return s


class TestDeleteDocument:
    def test_deletes_and_returns_count(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db") as mock_db,
            patch("quarry.mcp_server.db_delete_document", return_value=5) as mock_del,
        ):
            result = json.loads(delete_document("report.pdf"))

        mock_del.assert_called_once_with(mock_db.return_value, "report.pdf")
        assert result["document_name"] == "report.pdf"
        assert result["chunks_deleted"] == 5

    def test_returns_zero_for_missing(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.db_delete_document", return_value=0),
        ):
            result = json.loads(delete_document("nonexistent.pdf"))

        assert result["chunks_deleted"] == 0


class TestStatus:
    def test_returns_status_fields(self, tmp_path: Path):
        settings = _settings(tmp_path)
        # Create a fake DB directory with a file
        settings.lancedb_path.mkdir(parents=True)
        (settings.lancedb_path / "data.lance").write_bytes(b"x" * 1024)

        mock_docs = [{"document_name": "a.pdf"}, {"document_name": "b.pdf"}]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=mock_docs),
            patch("quarry.mcp_server.count_chunks", return_value=42),
        ):
            result = json.loads(status())

        assert result["document_count"] == 2
        assert result["chunk_count"] == 42
        assert result["database_path"] == str(settings.lancedb_path)
        assert result["database_size_bytes"] == 1024
        assert result["embedding_model"] == "Snowflake/snowflake-arctic-embed-m-v1.5"
        assert result["embedding_dimension"] == 768

    def test_empty_database(self, tmp_path: Path):
        settings = _settings(tmp_path)
        settings.lancedb_path.mkdir(parents=True)

        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=[]),
            patch("quarry.mcp_server.count_chunks", return_value=0),
        ):
            result = json.loads(status())

        assert result["document_count"] == 0
        assert result["chunk_count"] == 0
        assert result["database_size_bytes"] == 0

    def test_nonexistent_db_path(self, tmp_path: Path):
        settings = _settings(tmp_path)
        # Do NOT create lancedb_path — it should not exist

        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=[]),
            patch("quarry.mcp_server.count_chunks", return_value=0),
        ):
            result = json.loads(status())

        assert result["database_size_bytes"] == 0
