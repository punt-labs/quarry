from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from quarry.mcp_server import (
    delete,
    deregister_directory,
    find,
    ingest,
    list_resources as mcp_list,
    register_directory,
    remember as mcp_remember,
    show,
    status,
    sync_all_registrations,
    use_database,
)
from quarry.results import SearchResult


def _settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.lancedb_path = tmp_path / "lancedb"
    s.registry_path = tmp_path / "registry.db"
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.embedding_dimension = 768
    s.quarry_root = tmp_path
    return s


class TestRemember:
    def test_returns_immediately_with_background(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        started = threading.Event()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_content",
                side_effect=lambda *a, **kw: started.set(),
            ),
        ):
            result = mcp_remember("# Hello\nWorld", "notes.md")
            assert "notes.md" in result
            assert "background" in result
            assert started.wait(timeout=2), "background thread did not run"

    def test_passes_format_hint(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        done = threading.Event()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_content",
                side_effect=lambda *a, **kw: done.set(),
            ) as mock_ingest,
        ):
            mcp_remember("text", "a.txt", format_hint="markdown")
            assert done.wait(timeout=2), "background thread did not run"
            call_kwargs = mock_ingest.call_args[1]
            assert call_kwargs["format_hint"] == "markdown"

    def test_passes_collection(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        done = threading.Event()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_content",
                side_effect=lambda *a, **kw: done.set(),
            ) as mock_ingest,
        ):
            mcp_remember("text", "a.txt", collection="ml-101")
            assert done.wait(timeout=2), "background thread did not run"
            call_kwargs = mock_ingest.call_args[1]
            assert call_kwargs["collection"] == "ml-101"

    def test_remember_scrubs_content(self, tmp_path: Path) -> None:
        """The stdio remember scrubs PII before storing, like the daemon path."""
        settings = _settings(tmp_path)
        done = threading.Event()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_content",
                side_effect=lambda *a, **kw: done.set(),
            ) as mock_ingest,
        ):
            mcp_remember("reach me at jmf@pobox.com", "note.md")
            assert done.wait(timeout=2), "background thread did not run"

        scrub = mock_ingest.call_args[1]["content_scrubber"]
        redacted = scrub("reach me at jmf@pobox.com")
        assert "jmf@pobox.com" not in redacted
        assert "[REDACTED:email]" in redacted


class TestDeleteDocument:
    def test_returns_immediately_with_background(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        done = threading.Event()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_store.ChunkStore.delete_document",
                side_effect=lambda *a, **kw: done.set(),
            ),
        ):
            result = delete("report.pdf")
            assert "report.pdf" in result
            assert "background" in result
            assert done.wait(timeout=2), "background thread did not run"

    def test_invalid_kind(self) -> None:
        result = delete("x", kind="bogus")
        assert "Invalid kind" in result

    def test_scoped_to_collection(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        done = threading.Event()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_store.ChunkStore.delete_document",
                side_effect=lambda *a, **kw: done.set(),
            ) as mock_del,
        ):
            result = delete("report.pdf", collection="math")
            assert done.wait(timeout=2), "background thread did not run"
            mock_del.assert_called_once()
            assert mock_del.call_args[1]["collection"] == "math"
            assert "report.pdf" in result


class TestStatus:
    def test_returns_status_fields(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.lancedb_path.mkdir(parents=True)
        (settings.lancedb_path / "data.lance").write_bytes(b"x" * 1024)
        settings.registry_path.touch()

        mock_docs = [{"document_name": "a.pdf"}, {"document_name": "b.pdf"}]
        mock_cols = [{"collection": "math", "document_count": 2, "chunk_count": 42}]
        mock_conn = MagicMock()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
                return_value=mock_docs,
            ),
            patch("quarry.db.chunk_store.ChunkStore.count", return_value=42),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections",
                return_value=mock_cols,
            ),
            patch("quarry.mcp_server.SyncRegistry", return_value=mock_conn),
        ):
            mock_conn.list_registrations.return_value = ["fake"]
            result = status()

        assert "Documents:" in result
        assert "2" in result
        assert "Collections:" in result
        assert "1" in result
        assert "Chunks:" in result
        assert "42" in result
        assert "Directories:" in result
        assert str(settings.lancedb_path) in result
        assert "snowflake-arctic-embed-m-v1.5" in result

    def test_empty_database(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        settings.lancedb_path.mkdir(parents=True)
        settings.registry_path.touch()

        mock_conn = MagicMock()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
            patch("quarry.db.chunk_store.ChunkStore.count", return_value=0),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections", return_value=[]
            ),
            patch("quarry.mcp_server.SyncRegistry", return_value=mock_conn),
        ):
            mock_conn.list_registrations.return_value = []
            result = status()

        assert "Documents:      0" in result
        assert "Collections:    0" in result
        assert "Chunks:         0" in result
        assert "Directories:    0" in result

    def test_nonexistent_db_path(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        # registry_path doesn't exist → status() skips SyncRegistry
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
            patch("quarry.db.chunk_store.ChunkStore.count", return_value=0),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections", return_value=[]
            ),
        ):
            result = status()

        assert "Directories:    0" in result


def _mock_embedding_backend(mock_vector: np.ndarray) -> MagicMock:
    backend = MagicMock()
    backend.embed_query.return_value = mock_vector
    return backend


class TestFind:
    def test_returns_results(self, tmp_path: Path) -> None:
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
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch(
                "quarry.retrieval.hybrid.HybridRetriever.retrieve",
                return_value=[SearchResult.from_row(r) for r in mock_results],
            ),
        ):
            result = find("revenue growth")

        assert "revenue growth" in result
        assert "1 result" in result
        assert "report.pdf" in result
        assert "p3" in result
        assert "0.85" in result
        assert "quarterly revenue grew" in result

    def test_clamps_limit_to_50(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        mock_vector = np.zeros(768, dtype=np.float32)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch(
                "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
            ) as mock_search,
        ):
            find("test", limit=100)

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
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch(
                "quarry.retrieval.hybrid.HybridRetriever.retrieve", return_value=[]
            ) as mock_search,
        ):
            find("test", **{tool_kwarg: tool_value})

        # The retriever receives a SearchFilter; map the legacy "<name>_filter"
        # kwarg to the SearchFilter field name.
        search_filter = mock_search.call_args.kwargs["search_filter"]
        field = expected_key.removesuffix("_filter")
        assert getattr(search_filter, field) == expected_value

    def test_passes_document_filter(self, tmp_path: Path) -> None:
        self._assert_filter_passthrough(
            tmp_path,
            "document_filter",
            "report.pdf",
            "document_filter",
            "report.pdf",
        )

    def test_empty_filter_passes_none(self, tmp_path: Path) -> None:
        self._assert_filter_passthrough(
            tmp_path,
            "document_filter",
            "",
            "document_filter",
            None,
        )

    def test_passes_collection_filter(self, tmp_path: Path) -> None:
        self._assert_filter_passthrough(
            tmp_path,
            "collection",
            "math",
            "collection_filter",
            "math",
        )

    def test_passes_page_type_filter(self, tmp_path: Path) -> None:
        self._assert_filter_passthrough(
            tmp_path,
            "page_type",
            "code",
            "page_type_filter",
            "code",
        )

    def test_empty_page_type_passes_none(self, tmp_path: Path) -> None:
        self._assert_filter_passthrough(
            tmp_path,
            "page_type",
            "",
            "page_type_filter",
            None,
        )

    def test_passes_source_format_filter(self, tmp_path: Path) -> None:
        self._assert_filter_passthrough(
            tmp_path,
            "source_format",
            ".py",
            "source_format_filter",
            ".py",
        )

    def test_empty_source_format_passes_none(self, tmp_path: Path) -> None:
        self._assert_filter_passthrough(
            tmp_path,
            "source_format",
            "",
            "source_format_filter",
            None,
        )

    def test_results_include_metadata_fields(self, tmp_path: Path) -> None:
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
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.get_embedding_backend",
                return_value=_mock_embedding_backend(mock_vector),
            ),
            patch(
                "quarry.retrieval.hybrid.HybridRetriever.retrieve",
                return_value=[SearchResult.from_row(r) for r in mock_results],
            ),
        ):
            result = find("test")

        assert "script.py" in result
        assert "def main():" in result


class TestListDocuments:
    def test_returns_document_table(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        mock_docs = [
            {
                "document_name": "a.pdf",
                "collection": "math",
                "total_pages": 10,
                "chunk_count": 25,
            },
            {
                "document_name": "b.pdf",
                "collection": "math",
                "total_pages": 5,
                "chunk_count": 12,
            },
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
                return_value=mock_docs,
            ),
        ):
            result = mcp_list("documents")

        assert "a.pdf" in result
        assert "b.pdf" in result
        assert "DOCUMENT" in result

    def test_empty_database(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
        ):
            result = mcp_list("documents")

        assert "No documents" in result

    def test_filters_by_collection(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ) as mock_list,
        ):
            mcp_list("documents", collection="math")

        call_kwargs = mock_list.call_args[1]
        assert call_kwargs["collection_filter"] == "math"


class TestShow:
    def test_returns_page_text(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.get_page_text",
                return_value="The quick brown fox",
            ),
        ):
            result = show("report.pdf", page_number=3)

        assert "report.pdf" in result
        assert "Page: 3" in result
        assert "The quick brown fox" in result

    def test_returns_not_found_message(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.get_page_text", return_value=None
            ),
        ):
            result = show("missing.pdf", page_number=99)

        assert "No data found" in result
        assert "missing.pdf" in result

    def test_returns_metadata_without_page(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
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
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents",
                return_value=[mock_doc],
            ),
        ):
            result = show("report.pdf")

        assert "report.pdf" in result
        assert "math" in result
        assert "10" in result

    def test_metadata_not_found(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_documents", return_value=[]
            ),
        ):
            result = show("missing.pdf")

        assert "not found" in result


class TestListCollections:
    def test_returns_collections(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        mock_cols = [
            {"collection": "math", "document_count": 5, "chunk_count": 100},
            {"collection": "science", "document_count": 3, "chunk_count": 60},
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_catalog.ChunkCatalog.list_collections",
                return_value=mock_cols,
            ),
        ):
            result = mcp_list("collections")

        assert "math" in result
        assert "science" in result
        assert "COLLECTION" in result


class TestDeleteCollection:
    def test_deletes_collection_in_background(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        done = threading.Event()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.db.chunk_store.ChunkStore.delete_collection",
                side_effect=lambda *a, **kw: done.set(),
            ) as mock_del,
        ):
            result = delete("math", kind="collection")
            assert done.wait(timeout=2), "background thread did not run"
            mock_del.assert_called_once()
            assert "math" in result
            assert "background" in result


class TestHandleErrors:
    def test_returns_error_on_background_spawn_failure(self, tmp_path: Path) -> None:
        """_handle_errors catches exceptions before background spawn."""
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server._background",
                side_effect=RuntimeError("thread pool exhausted"),
            ),
        ):
            result = ingest("/tmp/bad.pdf")

        assert result.startswith("Error:")
        assert "RuntimeError" in result

    def test_background_thread_logs_exception(self, tmp_path: Path) -> None:
        """Exceptions inside the background thread are logged, not raised."""
        settings = _settings(tmp_path)
        done = threading.Event()

        def _failing_ingest(*args: object, **kwargs: object) -> None:
            done.set()
            msg = "bad format hint"
            raise ValueError(msg)

        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch(
                "quarry.mcp_server.pipeline_ingest_content",
                side_effect=_failing_ingest,
            ),
            patch("quarry.mcp_server.logger") as mock_logger,
        ):
            result = mcp_remember("text", "doc.txt")
            assert done.wait(timeout=2), "background thread did not run"

        # Tool itself returned successfully (optimistic response)
        assert "doc.txt" in result
        assert "background" in result
        # Background exception was logged via logger.exception
        mock_logger.exception.assert_called_once()


class TestRegisterDirectory:
    def test_returns_summary_immediately(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        done = threading.Event()
        d = tmp_path / "course"
        d.mkdir()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch(
                "quarry.mcp_server._background",
                side_effect=lambda *a, **kw: done.set(),
            ),
        ):
            result = register_directory(str(d), "my-course")
        assert "my-course" in result
        assert "background" in result
        assert str(d.resolve()) in result
        assert done.is_set()

    def test_default_collection_from_dir_name(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        d = tmp_path / "ml-101"
        d.mkdir()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._background"),
        ):
            result = register_directory(str(d))
        assert "ml-101" in result


class TestDeregisterDirectory:
    """MCP deregister runs synchronously: existence check, delete, purge."""

    @staticmethod
    def _register_with_file(
        settings: MagicMock, collection: str, tmp_path: Path
    ) -> None:
        from quarry.sync_registry import SyncRegistry

        directory = tmp_path / collection
        directory.mkdir()
        conn = SyncRegistry(settings.registry_path)
        try:
            conn.register_directory(directory, collection)
            conn.execute(
                "INSERT INTO files (path, collection, document_name, mtime, size, "
                "ingested_at, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(directory / "a.pdf"), collection, "a.pdf", 1.0, 10, "2025", None),
            )
            conn.commit()
        finally:
            conn.close()

    def test_deregister_synchronous_reports_counts(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._register_with_file(settings, "math", tmp_path)
        database = MagicMock()
        database.store.delete_document.return_value = 5
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._database", return_value=database),
        ):
            result = deregister_directory("math")
        assert "Deregistered collection 'math'" in result
        assert "1 files" in result
        assert "5 chunks" in result
        database.store.delete_document.assert_called_once_with(
            "a.pdf", collection="math"
        )

    def test_deregister_nonexistent_reports_not_found(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        database = MagicMock()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._database", return_value=database),
        ):
            result = deregister_directory("ghost")
        assert result == "No registration found for 'ghost'"
        database.store.delete_document.assert_not_called()

    def test_deregister_purge_failure_surfaces_error(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._register_with_file(settings, "math", tmp_path)
        database = MagicMock()
        database.store.delete_document.side_effect = RuntimeError("purge boom")
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._database", return_value=database),
        ):
            result = deregister_directory("math")
        assert result.startswith("Error")
        assert "Deregistered" not in result

    def test_deregister_keep_data_skips_purge(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        self._register_with_file(settings, "math", tmp_path)
        database = MagicMock()
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server._database", return_value=database),
        ):
            result = deregister_directory("math", keep_data=True)
        assert "Deregistered collection 'math'" in result
        assert "0 chunks" in result
        database.store.delete_document.assert_not_called()


class TestSyncAllRegistrations:
    def test_returns_immediately_with_background(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.db.facade.get_db"),
            patch("quarry.mcp_server._background") as mock_bg,
        ):
            result = sync_all_registrations()
        assert "Syncing" in result
        assert "background" in result
        mock_bg.assert_called_once()


class TestListRegistrations:
    def test_returns_registrations(self, tmp_path: Path) -> None:
        from quarry.sync_registry import SyncRegistry

        settings = _settings(tmp_path)
        d = tmp_path / "course"
        d.mkdir()
        # Register directly (not via fire-and-forget MCP tool)
        conn = SyncRegistry(settings.registry_path)
        try:
            conn.register_directory(d, "course")
        finally:
            conn.close()
        with patch("quarry.mcp_server._settings", return_value=settings):
            result = mcp_list("registrations")
        assert "course" in result
        assert "COLLECTION" in result

    def test_empty(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with patch("quarry.mcp_server._settings", return_value=settings):
            result = mcp_list("registrations")
        assert "No registered directories" in result


class TestDbNamePropagation:
    def test_settings_uses_db_name(self, tmp_path: Path) -> None:
        """Verify _settings() forwards the module _db_name."""
        import quarry.mcp_server as mcp_mod

        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set("work")
            mock_loaded = MagicMock()
            mock_loaded.resolve_db_paths.return_value = _settings(tmp_path)
            with patch(
                "quarry.mcp_server.Settings.load",
                return_value=mock_loaded,
            ):
                mcp_mod._settings()
            mock_loaded.resolve_db_paths.assert_called_once_with("work")
        finally:
            mcp_mod._db_name.set(original)

    def test_settings_default_none(self, tmp_path: Path) -> None:
        """Without db_name set, resolve_db_paths receives None."""
        import quarry.mcp_server as mcp_mod

        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set(None)
            mock_loaded = MagicMock()
            mock_loaded.resolve_db_paths.return_value = _settings(tmp_path)
            with patch(
                "quarry.mcp_server.Settings.load",
                return_value=mock_loaded,
            ):
                mcp_mod._settings()
            mock_loaded.resolve_db_paths.assert_called_once_with(None)
        finally:
            mcp_mod._db_name.set(original)


class TestListDatabases:
    def test_returns_databases(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        mock_dbs = [
            {
                "name": "default",
                "document_count": 5,
                "size_bytes": 1024,
                "size_description": "1.0 KB",
            },
            {
                "name": "coding",
                "document_count": 100,
                "size_bytes": 52428800,
                "size_description": "50.0 MB",
            },
        ]
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server.discover_databases", return_value=mock_dbs),
        ):
            result = mcp_list("databases")

        assert "default" in result
        assert "coding" in result
        assert "DATABASE" in result

    def test_marks_current_database(self, tmp_path: Path) -> None:
        import quarry.mcp_server as mcp_mod

        settings = _settings(tmp_path)
        mock_dbs = [
            {"name": "work", "document_count": 0, "size_bytes": 0},
        ]
        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set("work")
            with (
                patch("quarry.mcp_server._settings", return_value=settings),
                patch("quarry.mcp_server.discover_databases", return_value=mock_dbs),
            ):
                result = mcp_list("databases")
            assert "* work" in result
        finally:
            mcp_mod._db_name.set(original)

    def test_default_when_no_db_name(self, tmp_path: Path) -> None:
        import quarry.mcp_server as mcp_mod

        settings = _settings(tmp_path)
        mock_dbs = [
            {"name": "default", "document_count": 0, "size_bytes": 0},
        ]
        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set(None)
            with (
                patch("quarry.mcp_server._settings", return_value=settings),
                patch("quarry.mcp_server.discover_databases", return_value=mock_dbs),
            ):
                result = mcp_list("databases")
            assert "* default" in result
        finally:
            mcp_mod._db_name.set(original)

    def test_empty_root(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        with (
            patch("quarry.mcp_server._settings", return_value=settings),
            patch("quarry.mcp_server.discover_databases", return_value=[]),
        ):
            result = mcp_list("databases")
        assert "No databases" in result


class TestUseDatabase:
    def test_switches_database(self, tmp_path: Path) -> None:
        import quarry.mcp_server as mcp_mod

        settings = _settings(tmp_path)
        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set(None)
            with patch("quarry.mcp_server._settings", return_value=settings):
                result = use_database("coding")
            assert "default" in result
            assert "coding" in result
            assert mcp_mod._db_name.get() == "coding"
        finally:
            mcp_mod._db_name.set(original)

    def test_switches_back_to_default(self, tmp_path: Path) -> None:
        import quarry.mcp_server as mcp_mod

        settings = _settings(tmp_path)
        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set("coding")
            with patch("quarry.mcp_server._settings", return_value=settings):
                result = use_database("default")
            assert "coding" in result
            assert "default" in result
            assert mcp_mod._db_name.get() is None
        finally:
            mcp_mod._db_name.set(original)

    def test_returns_database_path(self, tmp_path: Path) -> None:
        import quarry.mcp_server as mcp_mod

        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set(None)
            result = use_database("work")
            assert "lancedb" in result
        finally:
            mcp_mod._db_name.set(original)

    def test_switch_between_named_databases(self, tmp_path: Path) -> None:
        import quarry.mcp_server as mcp_mod

        settings = _settings(tmp_path)
        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set("coding")
            with patch("quarry.mcp_server._settings", return_value=settings):
                result = use_database("work")
            assert "coding" in result
            assert "work" in result
            assert mcp_mod._db_name.get() == "work"
        finally:
            mcp_mod._db_name.set(original)

    def test_invalid_name_does_not_corrupt_state(self, tmp_path: Path) -> None:
        import quarry.mcp_server as mcp_mod

        original = mcp_mod._db_name.get()
        try:
            mcp_mod._db_name.set("good")
            result = use_database("../evil")
            assert "Error" in result
            assert mcp_mod._db_name.get() == "good"
        finally:
            mcp_mod._db_name.set(original)


class TestContextVarIsolation:
    """ContextVar must not leak between concurrent asyncio tasks.

    Each MCP session runs in its own asyncio Task.  If session A
    calls use_database("work"), session B must still see the default.
    """

    def test_db_name_isolated_across_tasks(self) -> None:
        import asyncio

        import quarry.mcp_server as mcp_mod

        original = mcp_mod._db_name.get()
        barrier = asyncio.Barrier(2)

        async def session_a() -> str | None:
            mcp_mod._db_name.set("work")
            await barrier.wait()  # sync with B
            await barrier.wait()  # wait for B to read
            return mcp_mod._db_name.get()

        async def session_b() -> str | None:
            await barrier.wait()  # A has set "work"
            value = mcp_mod._db_name.get()
            await barrier.wait()  # let A continue
            return value

        async def run() -> tuple[str | None, str | None]:
            a, b = await asyncio.gather(
                asyncio.create_task(session_a()),
                asyncio.create_task(session_b()),
            )
            return a, b

        try:
            a_val, b_val = asyncio.run(run())
            assert a_val == "work", "Task A should see its own mutation"
            assert b_val is None, "Task B should NOT see Task A's mutation"
        finally:
            mcp_mod._db_name.set(original)
