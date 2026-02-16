from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from quarry.sync import (
    _DEFAULT_IGNORE_PATTERNS,
    _load_ignore_spec,
    compute_sync_plan,
    discover_files,
    sync_all,
    sync_collection,
)
from quarry.sync_registry import (
    FileRecord,
    get_file,
    list_files,
    open_registry,
    register_directory,
    upsert_file,
)


class TestDiscoverFiles:
    def test_finds_supported_files(self, tmp_path: Path):
        (tmp_path / "a.pdf").touch()
        (tmp_path / "b.txt").touch()
        (tmp_path / "c.xyz").touch()
        exts = frozenset({".pdf", ".txt"})
        result = discover_files(tmp_path, exts)
        names = [p.name for p in result]
        assert "a.pdf" in names
        assert "b.txt" in names
        assert "c.xyz" not in names

    def test_recursive_discovery(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.pdf").touch()
        exts = frozenset({".pdf"})
        result = discover_files(tmp_path, exts)
        assert len(result) == 1
        assert result[0].name == "deep.pdf"

    def test_ignores_unsupported(self, tmp_path: Path):
        (tmp_path / "notes.log").touch()
        (tmp_path / "data.csv").touch()
        result = discover_files(tmp_path, frozenset({".pdf"}))
        assert result == []

    def test_empty_directory(self, tmp_path: Path):
        result = discover_files(tmp_path, frozenset({".pdf", ".txt"}))
        assert result == []

    def test_returns_sorted_absolute_paths(self, tmp_path: Path):
        (tmp_path / "z.pdf").touch()
        (tmp_path / "a.pdf").touch()
        result = discover_files(tmp_path, frozenset({".pdf"}))
        assert len(result) == 2
        assert result[0].name == "a.pdf"
        assert result[1].name == "z.pdf"
        assert all(p.is_absolute() for p in result)

    def test_skips_resource_fork_files(self, tmp_path: Path):
        (tmp_path / "report.pdf").touch()
        (tmp_path / "._report.pdf").touch()
        result = discover_files(tmp_path, frozenset({".pdf"}))
        assert len(result) == 1
        assert result[0].name == "report.pdf"

    def test_skips_trash_directory(self, tmp_path: Path):
        trash = tmp_path / ".Trash"
        trash.mkdir()
        (trash / "deleted.pdf").touch()
        (tmp_path / "keep.pdf").touch()
        result = discover_files(tmp_path, frozenset({".pdf"}))
        assert len(result) == 1
        assert result[0].name == "keep.pdf"

    def test_skips_dotfiles_in_subdirs(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "._hidden.pdf").touch()
        (sub / "visible.pdf").touch()
        result = discover_files(tmp_path, frozenset({".pdf"}))
        assert len(result) == 1
        assert result[0].name == "visible.pdf"

    def test_skips_files_in_hidden_directories(self, tmp_path: Path):
        hidden = tmp_path / ".git"
        hidden.mkdir()
        (hidden / "config.txt").touch()
        (tmp_path / "notes.txt").touch()
        result = discover_files(tmp_path, frozenset({".txt"}))
        assert len(result) == 1
        assert result[0].name == "notes.txt"

    def test_skips_venv_by_default(self, tmp_path: Path):
        venv = tmp_path / "venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "module.py").touch()
        (tmp_path / "app.py").touch()
        result = discover_files(tmp_path, frozenset({".py"}))
        assert len(result) == 1
        assert result[0].name == "app.py"

    def test_skips_node_modules_by_default(self, tmp_path: Path):
        nm = tmp_path / "node_modules" / "lodash"
        nm.mkdir(parents=True)
        (nm / "index.js").touch()
        (tmp_path / "app.js").touch()
        # .js not in default SUPPORTED_EXTENSIONS, use .txt for simplicity
        (tmp_path / "node_modules" / "readme.txt").touch()
        (tmp_path / "readme.txt").touch()
        result = discover_files(tmp_path, frozenset({".txt"}))
        assert len(result) == 1
        assert result[0].name == "readme.txt"

    def test_skips_pycache_by_default(self, tmp_path: Path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-313.pyc").touch()
        (tmp_path / "module.py").touch()
        result = discover_files(tmp_path, frozenset({".py", ".pyc"}))
        assert len(result) == 1
        assert result[0].name == "module.py"

    def test_respects_gitignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("data/\n*.log\n")
        data = tmp_path / "data"
        data.mkdir()
        (data / "big.csv").touch()
        (tmp_path / "debug.log").touch()
        (tmp_path / "app.txt").touch()
        result = discover_files(tmp_path, frozenset({".csv", ".log", ".txt"}))
        assert len(result) == 1
        assert result[0].name == "app.txt"

    def test_respects_quarryignore(self, tmp_path: Path):
        (tmp_path / ".quarryignore").write_text("archive/\n")
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "old.pdf").touch()
        (tmp_path / "new.pdf").touch()
        result = discover_files(tmp_path, frozenset({".pdf"}))
        assert len(result) == 1
        assert result[0].name == "new.pdf"

    def test_gitignore_and_quarryignore_combined(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / ".quarryignore").write_text("scratch/\n")
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        (scratch / "notes.txt").touch()
        (tmp_path / "debug.log").touch()
        (tmp_path / "app.txt").touch()
        result = discover_files(tmp_path, frozenset({".txt", ".log"}))
        assert len(result) == 1
        assert result[0].name == "app.txt"

    def test_gitignore_negation(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.txt\n!important.txt\n")
        (tmp_path / "notes.txt").touch()
        (tmp_path / "important.txt").touch()
        result = discover_files(tmp_path, frozenset({".txt"}))
        assert len(result) == 1
        assert result[0].name == "important.txt"

    def test_deeply_nested_venv_skipped(self, tmp_path: Path):
        deep = tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "numpy"
        deep.mkdir(parents=True)
        (deep / "core.py").touch()
        (tmp_path / "main.py").touch()
        result = discover_files(tmp_path, frozenset({".py"}))
        assert len(result) == 1
        assert result[0].name == "main.py"


class TestLoadIgnoreSpec:
    def test_default_patterns_present(self):
        assert "venv/" in _DEFAULT_IGNORE_PATTERNS
        assert "node_modules/" in _DEFAULT_IGNORE_PATTERNS
        assert "__pycache__/" in _DEFAULT_IGNORE_PATTERNS

    def test_loads_gitignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.log\noutput/\n")
        spec = _load_ignore_spec(tmp_path)
        assert spec.match_file("debug.log")
        assert spec.match_file("output/")
        assert not spec.match_file("app.py")

    def test_loads_quarryignore(self, tmp_path: Path):
        (tmp_path / ".quarryignore").write_text("scratch/\n")
        spec = _load_ignore_spec(tmp_path)
        assert spec.match_file("scratch/")

    def test_no_ignore_files_uses_defaults(self, tmp_path: Path):
        spec = _load_ignore_spec(tmp_path)
        assert spec.match_file("venv/")
        assert spec.match_file("node_modules/")
        assert not spec.match_file("src/app.py")

    def test_comments_and_blanks_ignored(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("# comment\n\n*.log\n")
        spec = _load_ignore_spec(tmp_path)
        assert spec.match_file("debug.log")
        assert not spec.match_file("# comment")


class TestComputeSyncPlan:
    EXTS = frozenset({".pdf", ".txt"})

    def _setup(self, tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
        """Create registry, docs directory, and register collection 'col'."""
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()
        register_directory(conn, d, "col")
        return conn, d

    def test_new_file_detected(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        (d / "new.pdf").write_bytes(b"data")
        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "new.pdf"
        assert plan.to_delete == []
        assert plan.unchanged == 0
        conn.close()

    def test_unchanged_file_skipped(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        f = d / "existing.pdf"
        f.write_bytes(b"data")
        stat = f.stat()
        upsert_file(
            conn,
            FileRecord(
                path=str(f.resolve()),
                collection="col",
                document_name="existing.pdf",
                mtime=stat.st_mtime,
                size=stat.st_size,
                ingested_at="2025-01-01",
            ),
        )
        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert plan.to_ingest == []
        assert plan.unchanged == 1
        conn.close()

    def test_changed_file_detected(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        f = d / "changed.pdf"
        f.write_bytes(b"old")
        upsert_file(
            conn,
            FileRecord(
                path=str(f.resolve()),
                collection="col",
                document_name="changed.pdf",
                mtime=f.stat().st_mtime,
                size=f.stat().st_size,
                ingested_at="2025-01-01",
            ),
        )
        # Modify the file and force a distinct mtime via os.utime
        f.write_bytes(b"new content that is longer")
        os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "changed.pdf"
        conn.close()

    def test_deleted_file_detected(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        upsert_file(
            conn,
            FileRecord(
                path=str((d / "gone.pdf").resolve()),
                collection="col",
                document_name="gone.pdf",
                mtime=100.0,
                size=500,
                ingested_at="2025-01-01",
            ),
        )
        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert plan.to_delete == ["gone.pdf"]
        conn.close()

    def test_mixed_scenario(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)

        # Unchanged file
        unch = d / "unchanged.pdf"
        unch.write_bytes(b"same")
        upsert_file(
            conn,
            FileRecord(
                path=str(unch.resolve()),
                collection="col",
                document_name="unchanged.pdf",
                mtime=unch.stat().st_mtime,
                size=unch.stat().st_size,
                ingested_at="2025-01-01",
            ),
        )

        # New file
        (d / "brand-new.txt").write_bytes(b"hello")

        # Deleted file (in registry but not on disk)
        upsert_file(
            conn,
            FileRecord(
                path=str((d / "removed.pdf").resolve()),
                collection="col",
                document_name="removed.pdf",
                mtime=100.0,
                size=500,
                ingested_at="2025-01-01",
            ),
        )

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "brand-new.txt"
        assert plan.to_delete == ["removed.pdf"]
        assert plan.unchanged == 1
        conn.close()


def _mock_settings(tmp_path: Path) -> MagicMock:
    s = MagicMock()
    s.registry_path = tmp_path / "registry.db"
    s.lancedb_path = tmp_path / "lancedb"
    s.embedding_model = "Snowflake/snowflake-arctic-embed-m-v1.5"
    s.chunk_max_chars = 1800
    s.chunk_overlap_chars = 200
    return s


class TestSyncCollection:
    def _setup(self, tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
        """Create registry, docs directory, and register collection 'col'."""
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()
        register_directory(conn, d, "col")
        return conn, d

    def test_ingests_new_files(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        (d / "a.txt").write_text("hello")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        with patch("quarry.sync.ingest_document") as mock_ingest:
            mock_ingest.return_value = {"chunks": 2}
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)

        assert result.ingested == 1
        assert result.failed == 0
        assert result.skipped == 0
        mock_ingest.assert_called_once()
        # Verify file record was created
        rec = get_file(conn, str((d / "a.txt").resolve()))
        assert rec is not None
        assert rec.collection == "col"
        conn.close()

    def test_error_isolation(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        (d / "good.txt").write_text("ok")
        (d / "bad.txt").write_text("fail")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        def side_effect(fp: Path, *args: object, **kwargs: object) -> dict[str, object]:
            if fp.name == "bad.txt":
                msg = "boom"
                raise RuntimeError(msg)
            return {"chunks": 1}

        with patch("quarry.sync.ingest_document", side_effect=side_effect):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)

        assert result.ingested == 1
        assert result.failed == 1
        assert len(result.errors) == 1
        assert "bad.txt" in result.errors[0]
        conn.close()

    def test_deletes_removed_files(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        # Register a file that no longer exists on disk
        upsert_file(
            conn,
            FileRecord(
                path=str((d / "gone.txt").resolve()),
                collection="col",
                document_name="gone.txt",
                mtime=100.0,
                size=50,
                ingested_at="2025-01-01",
            ),
        )

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        with (
            patch("quarry.sync.ingest_document"),
            patch("quarry.sync.delete_document") as mock_del,
        ):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)

        assert result.deleted == 1
        mock_del.assert_called_once_with(db, "gone.txt", collection="col")
        conn.close()

    def test_registry_updated_after_sync(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        (d / "new.txt").write_text("data")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        with patch("quarry.sync.ingest_document", return_value={"chunks": 1}):
            sync_collection(d, "col", db, settings, conn, max_workers=1)

        files = list_files(conn, "col")
        assert len(files) == 1
        assert files[0].document_name == "new.txt"
        conn.close()


class TestSyncAll:
    def test_syncs_all_registered(self, tmp_path: Path):
        settings = _mock_settings(tmp_path)
        conn = open_registry(settings.registry_path)
        d1 = tmp_path / "a"
        d1.mkdir()
        (d1 / "one.txt").write_text("hello")
        d2 = tmp_path / "b"
        d2.mkdir()
        (d2 / "two.txt").write_text("world")
        register_directory(conn, d1, "alpha")
        register_directory(conn, d2, "beta")
        conn.close()

        db = MagicMock()
        # Mock table operations used by create_collection_index and optimize_table
        db.list_tables.return_value.tables = []

        with patch("quarry.sync.ingest_document", return_value={"chunks": 1}):
            results = sync_all(db, settings, max_workers=1)

        assert "alpha" in results
        assert "beta" in results
        assert results["alpha"].ingested == 1
        assert results["beta"].ingested == 1
