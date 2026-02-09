from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from quarry.mcp_server import (
    delete_document,
    get_documents,
    get_page,
    ingest,
    ingest_text,
    search_documents,
    status,
)


def _settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    return s


class TestIngestText:
    def test_calls_pipeline_and_returns_progress(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_result = {"document_name": "notes.md", "chunks": 3, "sections": 2}
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_text",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            result = ingest_text("# Hello\nWorld", "notes.md")

        mock_ingest.assert_called_once()
        call_args = mock_ingest.call_args
        assert call_args[0][0] == "# Hello\nWorld"
        assert call_args[0][1] == "notes.md"
        assert "notes.md" in result
        assert "chunks" in result

    def test_passes_format_hint(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_result = {"document_name": "a.txt", "chunks": 1, "sections": 1}
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_text",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            ingest_text("text", "a.txt", format_hint="markdown")

        call_kwargs = mock_ingest.call_args[1]
        assert call_kwargs["format_hint"] == "markdown"


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


class TestSearchDocuments:
    def test_returns_results(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_results = [
            {
                "document_name": "report.pdf",
                "page_number": 3,
                "chunk_index": 0,
                "text": "quarterly revenue grew",
                "_distance": 0.15,
            },
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.embed_query", return_value=mock_vector),
            patch("quarry.mcp_server.search", return_value=mock_results),
        ):
            result = json.loads(search_documents("revenue growth"))

        assert result["query"] == "revenue growth"
        assert result["total_results"] == 1
        assert result["results"][0]["document_name"] == "report.pdf"
        assert result["results"][0]["similarity"] == 0.85

    def test_clamps_limit_to_50(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.embed_query", return_value=mock_vector),
            patch("quarry.mcp_server.search", return_value=[]) as mock_search,
        ):
            search_documents("test", limit=100)

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["limit"] == 50

    def test_passes_document_filter(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.embed_query", return_value=mock_vector),
            patch("quarry.mcp_server.search", return_value=[]) as mock_search,
        ):
            search_documents("test", document_filter="report.pdf")

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["document_filter"] == "report.pdf"

    def test_empty_filter_passes_none(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.embed_query", return_value=mock_vector),
            patch("quarry.mcp_server.search", return_value=[]) as mock_search,
        ):
            search_documents("test", document_filter="")

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["document_filter"] is None


class TestGetDocuments:
    def test_returns_document_list(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_docs = [
            {"document_name": "a.pdf", "total_pages": 10, "chunk_count": 25},
            {"document_name": "b.pdf", "total_pages": 5, "chunk_count": 12},
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=mock_docs),
        ):
            result = json.loads(get_documents())

        assert result["total_documents"] == 2
        assert result["documents"][0]["document_name"] == "a.pdf"

    def test_empty_database(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=[]),
        ):
            result = json.loads(get_documents())

        assert result["total_documents"] == 0
        assert result["documents"] == []


class TestGetPage:
    def test_returns_page_text(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.get_page_text",
                return_value="The quick brown fox",
            ),
        ):
            result = get_page("report.pdf", 3)

        assert "report.pdf" in result
        assert "Page: 3" in result
        assert "The quick brown fox" in result

    def test_returns_not_found_message(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.get_page_text", return_value=None),
        ):
            result = get_page("missing.pdf", 99)

        assert "No data found" in result
        assert "missing.pdf" in result


class TestHandleErrors:
    def test_returns_error_string_on_exception(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.ingest_document",
                side_effect=FileNotFoundError("no such file: bad.pdf"),
            ),
        ):
            result = ingest("/tmp/bad.pdf")

        assert result.startswith("Error:")
        assert "FileNotFoundError" in result
        assert "bad.pdf" in result

    def test_returns_error_on_value_error(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_text",
                side_effect=ValueError("bad format hint"),
            ),
        ):
            result = ingest_text("text", "doc.txt")

        assert "ValueError" in result
        assert "bad format hint" in result
