from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
from typer.testing import CliRunner

from quarry.__main__ import app

runner = CliRunner()


def _mock_settings() -> MagicMock:
    s = MagicMock()
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    return s


class TestListCmd:
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
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=mock_docs),
        ):
            result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "[default]" in result.output
        assert "report.pdf" in result.output
        assert "10/10 pages" in result.output
        assert "25 chunks" in result.output

    def test_filters_by_collection(self):
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]) as mock_list,
        ):
            runner.invoke(app, ["list", "--collection", "math"])

        call_kwargs = mock_list.call_args[1]
        assert call_kwargs["collection_filter"] == "math"

    def test_empty_database(self):
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.list_documents", return_value=[]),
        ):
            result = runner.invoke(app, ["list"])

        assert result.exit_code == 0
        assert "No documents indexed" in result.output


class TestDeleteCmd:
    def test_deletes_document(self):
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=15),
        ):
            result = runner.invoke(app, ["delete", "report.pdf"])

        assert result.exit_code == 0
        assert "Deleted 15 chunks" in result.output
        assert "report.pdf" in result.output

    def test_not_found(self):
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document", return_value=0),
        ):
            result = runner.invoke(app, ["delete", "missing.pdf"])

        assert result.exit_code == 0
        assert "No data found" in result.output


class TestSearchCmd:
    def test_prints_results(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_results = [
            {
                "document_name": "report.pdf",
                "page_number": 3,
                "text": "quarterly revenue grew 15%",
                "_distance": 0.15,
            },
        ]
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.embed_query", return_value=mock_vector),
            patch("quarry.__main__.search", return_value=mock_results),
        ):
            result = runner.invoke(app, ["search", "revenue growth"])

        assert result.exit_code == 0
        assert "report.pdf" in result.output
        assert "p.3" in result.output
        assert "quarterly revenue" in result.output

    def test_no_results(self):
        mock_vector = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.embed_query", return_value=mock_vector),
            patch("quarry.__main__.search", return_value=[]),
        ):
            result = runner.invoke(app, ["search", "nonexistent topic"])

        assert result.exit_code == 0


class TestCollectionsCmd:
    def test_lists_collections(self):
        mock_cols = [
            {"collection": "math", "document_count": 5, "chunk_count": 100},
            {"collection": "science", "document_count": 3, "chunk_count": 60},
        ]
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=mock_cols),
        ):
            result = runner.invoke(app, ["collections"])

        assert result.exit_code == 0
        assert "math" in result.output
        assert "5 documents" in result.output
        assert "100 chunks" in result.output
        assert "science" in result.output

    def test_empty(self):
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["collections"])

        assert result.exit_code == 0
        assert "No collections found" in result.output


class TestCliErrors:
    def test_error_exits_with_code_1(self):
        with (
            patch("quarry.__main__.get_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch(
                "quarry.__main__.list_documents",
                side_effect=RuntimeError("db corrupt"),
            ),
        ):
            result = runner.invoke(app, ["list"])

        assert result.exit_code == 1
