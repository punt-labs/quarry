from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from quarry.mcp_server import (
    delete_collection,
    delete_document,
    deregister_directory,
    get_documents,
    get_page,
    ingest,
    ingest_text as mcp_ingest_text,
    list_collections,
    list_registrations,
    register_directory,
    search_documents,
    status,
    sync_all_registrations,
)


def _settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.registry_path = tmp_path / "registry.db"
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
                "quarry.mcp_server.pipeline_ingest_text_content",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            result = mcp_ingest_text("# Hello\nWorld", "notes.md")

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
                "quarry.mcp_server.pipeline_ingest_text_content",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            mcp_ingest_text("text", "a.txt", format_hint="markdown")

        call_kwargs = mock_ingest.call_args[1]
        assert call_kwargs["format_hint"] == "markdown"

    def test_passes_collection(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_result = {"document_name": "a.txt", "chunks": 1, "sections": 1}
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_text_content",
                return_value=mock_result,
            ) as mock_ingest,
        ):
            mcp_ingest_text("text", "a.txt", collection="ml-101")

        call_kwargs = mock_ingest.call_args[1]
        assert call_kwargs["collection"] == "ml-101"


class TestDeleteDocument:
    def test_deletes_and_returns_count(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db") as mock_db,
            patch("quarry.mcp_server.db_delete_document", return_value=5) as mock_del,
        ):
            result = json.loads(delete_document("report.pdf"))

        mock_del.assert_called_once_with(
            mock_db.return_value, "report.pdf", collection=None
        )
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

    def test_scoped_to_collection(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db") as mock_db,
            patch("quarry.mcp_server.db_delete_document", return_value=2) as mock_del,
        ):
            result = json.loads(delete_document("report.pdf", collection="math"))

        mock_del.assert_called_once_with(
            mock_db.return_value, "report.pdf", collection="math"
        )
        assert result["collection"] == "math"


class TestStatus:
    def test_returns_status_fields(self, tmp_path: Path):
        settings = _settings(tmp_path)
        settings.lancedb_path.mkdir(parents=True)
        (settings.lancedb_path / "data.lance").write_bytes(b"x" * 1024)
        settings.registry_path.touch()

        mock_docs = [{"document_name": "a.pdf"}, {"document_name": "b.pdf"}]
        mock_cols = [{"collection": "math", "document_count": 2, "chunk_count": 42}]
        mock_conn = MagicMock()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=mock_docs),
            patch("quarry.mcp_server.count_chunks", return_value=42),
            patch("quarry.mcp_server.db_list_collections", return_value=mock_cols),
            patch("quarry.mcp_server.open_registry", return_value=mock_conn),
            patch("quarry.mcp_server.registry_list", return_value=["fake"]),
        ):
            result = json.loads(status())

        assert result["document_count"] == 2
        assert result["collection_count"] == 1
        assert result["chunk_count"] == 42
        assert result["registered_directories"] == 1
        assert result["database_path"] == str(settings.lancedb_path)
        assert result["database_size_bytes"] == 1024
        assert result["embedding_model"] == "Snowflake/snowflake-arctic-embed-m-v1.5"
        assert result["embedding_dimension"] == 768

    def test_empty_database(self, tmp_path: Path):
        settings = _settings(tmp_path)
        settings.lancedb_path.mkdir(parents=True)
        settings.registry_path.touch()

        mock_conn = MagicMock()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=[]),
            patch("quarry.mcp_server.count_chunks", return_value=0),
            patch("quarry.mcp_server.db_list_collections", return_value=[]),
            patch("quarry.mcp_server.open_registry", return_value=mock_conn),
            patch("quarry.mcp_server.registry_list", return_value=[]),
        ):
            result = json.loads(status())

        assert result["document_count"] == 0
        assert result["collection_count"] == 0
        assert result["chunk_count"] == 0
        assert result["registered_directories"] == 0
        assert result["database_size_bytes"] == 0

    def test_nonexistent_db_path(self, tmp_path: Path):
        settings = _settings(tmp_path)
        # registry_path doesn't exist → status() skips open_registry
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=[]),
            patch("quarry.mcp_server.count_chunks", return_value=0),
            patch("quarry.mcp_server.db_list_collections", return_value=[]),
        ):
            result = json.loads(status())

        assert result["database_size_bytes"] == 0
        assert result["registered_directories"] == 0


def _mock_embedding_backend(mock_vector: np.ndarray) -> MagicMock:
    backend = MagicMock()
    backend.embed_query.return_value = mock_vector
    return backend


class TestSearchDocuments:
    def test_returns_results(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_results = [
            {
                "document_name": "report.pdf",
                "collection": "finance",
                "page_number": 3,
                "chunk_index": 0,
                "text": "quarterly revenue grew",
                "page_type": "text",
                "source_format": ".pdf",
                "_distance": 0.15,
            },
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch("quarry.mcp_server.search", return_value=mock_results),
        ):
            result = json.loads(search_documents("revenue growth"))

        assert result["query"] == "revenue growth"
        assert result["total_results"] == 1
        assert result["results"][0]["document_name"] == "report.pdf"
        assert result["results"][0]["collection"] == "finance"
        assert result["results"][0]["similarity"] == 0.85

    def test_clamps_limit_to_50(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch("quarry.mcp_server.search", return_value=[]) as mock_search,
        ):
            search_documents("test", limit=100)

        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["limit"] == 50

    def _assert_filter_passthrough(
        self,
        tmp_path: Path,
        tool_kwarg: str,
        tool_value: str,
        expected_key: str,
        expected_value: str | None,
    ) -> None:
        """Call search_documents with one filter kwarg, assert it reaches search()."""
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch("quarry.mcp_server.search", return_value=[]) as mock_search,
        ):
            search_documents("test", **{tool_kwarg: tool_value})

        assert mock_search.call_args[1][expected_key] == expected_value

    def test_passes_document_filter(self, tmp_path: Path):
        self._assert_filter_passthrough(
            tmp_path,
            "document_filter",
            "report.pdf",
            "document_filter",
            "report.pdf",
        )

    def test_empty_filter_passes_none(self, tmp_path: Path):
        self._assert_filter_passthrough(
            tmp_path,
            "document_filter",
            "",
            "document_filter",
            None,
        )

    def test_passes_collection_filter(self, tmp_path: Path):
        self._assert_filter_passthrough(
            tmp_path,
            "collection",
            "math",
            "collection_filter",
            "math",
        )

    def test_passes_page_type_filter(self, tmp_path: Path):
        self._assert_filter_passthrough(
            tmp_path,
            "page_type",
            "code",
            "page_type_filter",
            "code",
        )

    def test_empty_page_type_passes_none(self, tmp_path: Path):
        self._assert_filter_passthrough(
            tmp_path,
            "page_type",
            "",
            "page_type_filter",
            None,
        )

    def test_passes_source_format_filter(self, tmp_path: Path):
        self._assert_filter_passthrough(
            tmp_path,
            "source_format",
            ".py",
            "source_format_filter",
            ".py",
        )

    def test_empty_source_format_passes_none(self, tmp_path: Path):
        self._assert_filter_passthrough(
            tmp_path,
            "source_format",
            "",
            "source_format_filter",
            None,
        )

    def test_results_include_metadata_fields(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        mock_results = [
            {
                "document_name": "script.py",
                "collection": "default",
                "page_number": 1,
                "chunk_index": 0,
                "text": "def main():",
                "page_type": "code",
                "source_format": ".py",
                "_distance": 0.1,
            }
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch("quarry.mcp_server.search", return_value=mock_results),
        ):
            result = json.loads(search_documents("test"))

        r = result["results"][0]
        assert r["page_type"] == "code"
        assert r["source_format"] == ".py"


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

    def test_filters_by_collection(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.list_documents", return_value=[]) as mock_list,
        ):
            get_documents(collection="math")

        call_kwargs = mock_list.call_args[1]
        assert call_kwargs["collection_filter"] == "math"


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


class TestListCollections:
    def test_returns_collections(self, tmp_path: Path):
        settings = _settings(tmp_path)
        mock_cols = [
            {"collection": "math", "document_count": 5, "chunk_count": 100},
            {"collection": "science", "document_count": 3, "chunk_count": 60},
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.db_list_collections", return_value=mock_cols),
        ):
            result = json.loads(list_collections())

        assert result["total_collections"] == 2
        assert result["collections"][0]["collection"] == "math"


class TestDeleteCollection:
    def test_deletes_collection(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db") as mock_db,
            patch(
                "quarry.mcp_server.db_delete_collection", return_value=50
            ) as mock_del,
        ):
            result = json.loads(delete_collection("math"))

        mock_del.assert_called_once_with(mock_db.return_value, "math")
        assert result["collection"] == "math"
        assert result["chunks_deleted"] == 50


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
                "quarry.mcp_server.pipeline_ingest_text_content",
                side_effect=ValueError("bad format hint"),
            ),
        ):
            result = mcp_ingest_text("text", "doc.txt")

        assert "ValueError" in result
        assert "bad format hint" in result


class TestRegisterDirectory:
    def test_registers_and_returns_json(self, tmp_path: Path):
        settings = _settings(tmp_path)
        d = tmp_path / "course"
        d.mkdir()
        with patch("quarry.mcp_server._settings", return_value=settings):
            result = json.loads(register_directory(str(d), "my-course"))
        assert result["collection"] == "my-course"
        assert result["directory"] == str(d.resolve())

    def test_default_collection_from_dir_name(self, tmp_path: Path):
        settings = _settings(tmp_path)
        d = tmp_path / "ml-101"
        d.mkdir()
        with patch("quarry.mcp_server._settings", return_value=settings):
            result = json.loads(register_directory(str(d)))
        assert result["collection"] == "ml-101"


class TestDeregisterDirectory:
    def test_deregisters_and_cleans_data(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server.open_registry") as mock_open,
            patch(
                "quarry.mcp_server.registry_deregister",
                return_value=["a.pdf", "b.pdf"],
            ),
            patch("quarry.mcp_server._db"),
            patch("quarry.mcp_server.db_delete_document") as mock_del,
        ):
            mock_open.return_value = MagicMock()
            result = json.loads(deregister_directory("math"))
        assert result["collection"] == "math"
        assert result["documents_removed"] == 2
        assert result["data_deleted"] is True
        assert mock_del.call_count == 2


class TestSyncAllRegistrations:
    def test_returns_sync_results(self, tmp_path: Path):
        from quarry.sync import SyncResult

        settings = _settings(tmp_path)
        mock_results = {
            "math": SyncResult(
                collection="math",
                ingested=2,
                deleted=0,
                skipped=3,
                failed=0,
            ),
        }
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._db"),
            patch(
                "quarry.mcp_server.engine_sync_all",
                return_value=mock_results,
            ),
        ):
            result = json.loads(sync_all_registrations())
        assert result["collections_synced"] == 1
        assert result["results"]["math"]["ingested"] == 2
        assert result["results"]["math"]["skipped"] == 3
        assert isinstance(result["progress"], list)


class TestListRegistrations:
    def test_returns_registrations(self, tmp_path: Path):
        settings = _settings(tmp_path)
        d = tmp_path / "course"
        d.mkdir()
        # Register first
        with patch("quarry.mcp_server._settings", return_value=settings):
            register_directory(str(d), "course")
            result = json.loads(list_registrations())
        assert result["total_registrations"] == 1
        assert result["registrations"][0]["collection"] == "course"

    def test_empty(self, tmp_path: Path):
        settings = _settings(tmp_path)
        with patch("quarry.mcp_server._settings", return_value=settings):
            result = json.loads(list_registrations())
        assert result["total_registrations"] == 0


class TestDbNamePropagation:
    def test_settings_uses_db_name(self, tmp_path: Path):
        """Verify _settings() calls resolve_db_paths with the module _db_name."""
        import quarry.mcp_server as mcp_mod

        original = mcp_mod._db_name
        try:
            mcp_mod._db_name = "work"
            with patch(
                "quarry.mcp_server.resolve_db_paths",
            ) as mock_resolve:
                mock_resolve.return_value = _settings(tmp_path)
                mcp_mod._settings()
            mock_resolve.assert_called_once()
            assert mock_resolve.call_args[0][1] == "work"
        finally:
            mcp_mod._db_name = original

    def test_settings_default_none(self, tmp_path: Path):
        """Without db_name set, resolve_db_paths receives None."""
        import quarry.mcp_server as mcp_mod

        original = mcp_mod._db_name
        try:
            mcp_mod._db_name = None
            with patch(
                "quarry.mcp_server.resolve_db_paths",
            ) as mock_resolve:
                mock_resolve.return_value = _settings(tmp_path)
                mcp_mod._settings()
            assert mock_resolve.call_args[0][1] is None
        finally:
            mcp_mod._db_name = original
