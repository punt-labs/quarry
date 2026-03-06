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
            patch("quarry.__main__.search", return_value=mock_results),
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
            patch("quarry.__main__.search", return_value=[]),
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
            patch("quarry.__main__.search", return_value=[]) as mock_search,
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
            patch("quarry.__main__.search", return_value=[]) as mock_search,
        ):
            result = runner.invoke(app, ["find", "query"])

        assert result.exit_code == 0
        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["document_filter"] is None
        assert call_kwargs["page_type_filter"] is None
        assert call_kwargs["source_format_filter"] is None


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
            patch("quarry.__main__.search", return_value=[]),
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

        settings = Settings.model_validate(
            {"ocr_backend": "local", "embedding_backend": "onnx"}
        )
        assert _auto_workers(settings) == 1

    def test_textract_ocr_triggers_four(self) -> None:
        from quarry.__main__ import _auto_workers
        from quarry.config import Settings

        settings = Settings.model_validate({"ocr_backend": "textract"})
        assert _auto_workers(settings) == 4

    def test_sagemaker_embedding_triggers_four(self) -> None:
        from quarry.__main__ import _auto_workers
        from quarry.config import Settings

        settings = Settings.model_validate({"embedding_backend": "sagemaker"})
        assert _auto_workers(settings) == 4

    def test_both_cloud_triggers_four(self) -> None:
        from quarry.__main__ import _auto_workers
        from quarry.config import Settings

        settings = Settings.model_validate(
            {"ocr_backend": "textract", "embedding_backend": "sagemaker"}
        )
        assert _auto_workers(settings) == 4


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
