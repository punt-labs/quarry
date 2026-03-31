from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from typer.testing import CliRunner

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
        assert "[default]" in result.output
        assert "report.pdf" in result.output
        assert "10/10 pages" in result.output
        assert "25 chunks" in result.output

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
        assert "No documents indexed" in result.output

    def test_bare_list_errors(self):
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 1


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
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_page_text", return_value=None),
        ):
            result = runner.invoke(app, ["show", "report.pdf", "--page", "0"])

        assert result.exit_code == 1
        assert "No data found" in result.output

    def test_show_negative_page(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.get_page_text", return_value=None),
        ):
            result = runner.invoke(app, ["show", "report.pdf", "--page", "-1"])

        assert result.exit_code == 1
        assert "No data found" in result.output

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

        assert result.exit_code == 0
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

        assert result.exit_code == 0
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
        assert "5 documents" in result.output
        assert "100 chunks" in result.output
        assert "science" in result.output

    def test_empty(self):
        with (
            patch("quarry.__main__._resolved_settings", return_value=_mock_settings()),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_list_collections", return_value=[]),
        ):
            result = runner.invoke(app, ["list", "collections"])

        assert result.exit_code == 0
        assert "No collections found" in result.output


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


class TestDeregisterCmd:
    def test_deregisters_collection(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.deregister_directory", return_value=["a.pdf"]),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document"),
        ):
            result = runner.invoke(app, ["deregister", "math"])
        assert result.exit_code == 0
        assert "Deregistered" in result.output
        assert "math" in result.output

    def test_keep_data_flag(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.deregister_directory", return_value=["a.pdf"]),
            patch("quarry.__main__.get_db") as mock_get_db,
            patch("quarry.__main__.db_delete_document") as mock_del,
        ):
            result = runner.invoke(app, ["deregister", "math", "--keep-data"])
        assert result.exit_code == 0
        mock_get_db.assert_not_called()
        mock_del.assert_not_called()

    def test_deregister_empty_collection(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
            patch("quarry.__main__.deregister_directory", return_value=[]),
        ):
            result = runner.invoke(app, ["deregister", "empty"])
        assert result.exit_code == 0
        assert "0 files" in result.output

    def test_deregister_delete_error(self, tmp_path: Path):
        settings = _mock_settings()
        settings.registry_path = tmp_path / "registry.db"
        with (
            patch("quarry.__main__._resolved_settings", return_value=settings),
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
            patch("quarry.__main__.db_delete_document", return_value=0),
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
                "text": "revenue grew",
                "page_type": "text",
                "source_format": ".pdf",
                "_distance": 0.15,
                "collection": "default",
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
        assert "similarity" in data[0]
        assert data[0]["text"] == "revenue grew"

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
        assert data["page"] == 2
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
            patch("quarry.__main__.deregister_directory", return_value=["a.pdf"]),
            patch("quarry.__main__.get_db"),
            patch("quarry.__main__.db_delete_document"),
        ):
            result = runner.invoke(app, ["--json", "deregister", "math"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["collection"] == "math"
        assert data["removed"] == 1

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


class TestLoginCmd:
    def test_success(self) -> None:
        with (
            patch(
                "quarry.__main__.validate_connection", return_value=(True, "")
            ) as mock_validate,
            patch("quarry.__main__.write_proxy_config") as mock_write,
        ):
            result = runner.invoke(
                app, ["login", "okinos.example.com", "--api-key", "sk-test"]
            )
        _reset_globals()
        assert result.exit_code == 0
        assert "Restart Claude Code" in result.output
        mock_validate.assert_called_once_with(
            "okinos.example.com", 8420, "sk-test", scheme="https"
        )
        mock_write.assert_called_once_with(
            "wss://okinos.example.com:8420/mcp", "sk-test"
        )

    def test_default_ws_for_localhost(self) -> None:
        with (
            patch(
                "quarry.__main__.validate_connection", return_value=(True, "")
            ) as mock_validate,
            patch("quarry.__main__.write_proxy_config") as mock_write,
        ):
            result = runner.invoke(app, ["login", "localhost", "--api-key", "sk-test"])
        _reset_globals()
        assert result.exit_code == 0
        mock_validate.assert_called_once_with(
            "localhost", 8420, "sk-test", scheme="http"
        )
        mock_write.assert_called_once_with("ws://localhost:8420/mcp", "sk-test")

    def test_insecure_flag_allows_ws_for_non_localhost(self) -> None:
        with (
            patch(
                "quarry.__main__.validate_connection", return_value=(True, "")
            ) as mock_validate,
            patch("quarry.__main__.write_proxy_config") as mock_write,
        ):
            result = runner.invoke(
                app,
                ["login", "okinos.example.com", "--api-key", "sk-test", "--insecure"],
            )
        _reset_globals()
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "cleartext" in result.output
        mock_validate.assert_called_once_with(
            "okinos.example.com", 8420, "sk-test", scheme="http"
        )
        mock_write.assert_called_once_with(
            "ws://okinos.example.com:8420/mcp", "sk-test"
        )

    def test_connection_failure(self) -> None:
        with (
            patch(
                "quarry.__main__.validate_connection",
                return_value=(False, "Authentication failed — check --api-key."),
            ),
            patch("quarry.__main__.write_proxy_config") as mock_write,
        ):
            result = runner.invoke(
                app, ["login", "okinos.example.com", "--api-key", "bad-key"]
            )
        _reset_globals()
        assert result.exit_code == 1
        assert "Authentication failed" in result.output
        mock_write.assert_not_called()

    def test_custom_port(self) -> None:
        with (
            patch("quarry.__main__.validate_connection", return_value=(True, "")),
            patch("quarry.__main__.write_proxy_config") as mock_write,
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
                ],
            )
        _reset_globals()
        assert result.exit_code == 0
        mock_write.assert_called_once_with(
            "wss://okinos.example.com:9000/mcp", "sk-test"
        )

    def test_empty_api_key_exits_with_error(self) -> None:
        with patch("quarry.__main__.validate_connection") as mock_validate:
            result = runner.invoke(app, ["login", "host.example.com", "--api-key", ""])
        _reset_globals()
        assert result.exit_code == 1
        assert "required" in result.output
        mock_validate.assert_not_called()


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

    def test_malformed_toml_shows_error(self) -> None:
        with patch(
            "quarry.__main__.read_proxy_config",
            side_effect=ValueError("Malformed config at /path/quarry.toml: ..."),
        ):
            result = runner.invoke(app, ["remote", "list"])
        _reset_globals()
        assert result.exit_code == 1
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
