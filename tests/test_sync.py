from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from quarry.models import Chunk
from quarry.sync import (
    _DEFAULT_IGNORE_PATTERNS,
    _content_hash,
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


def _fake_prepare(
    fp: Path,
    settings: object,
    **kwargs: object,
) -> tuple[list[Chunk], np.ndarray]:
    """Return a minimal (chunks, vectors) pair for testing."""
    from datetime import UTC, datetime

    doc_name = kwargs.get("document_name", fp.name)
    assert isinstance(doc_name, str)
    chunk = Chunk(
        document_name=doc_name,
        document_path=str(fp),
        collection=str(kwargs.get("collection", "default")),
        page_number=1,
        total_pages=1,
        chunk_index=0,
        text="test",
        page_raw_text="test",
        page_type="text",
        source_format=fp.suffix,
        ingestion_timestamp=datetime.now(UTC),
    )
    vectors = np.zeros((1, 768), dtype=np.float32)
    return [chunk], vectors


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

    def test_symlink_escape_is_dropped(self, tmp_path: Path):
        """A symlink whose target resolves outside the root must be skipped.

        Without this check a remote client could register ``~/docs``
        containing ``shadow -> /etc/shadow`` and have the target's
        contents ingested into the searchable index.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("top secret")

        root = tmp_path / "root"
        root.mkdir()
        (root / "legit.txt").write_text("hello")
        (root / "escape.txt").symlink_to(secret)

        result = discover_files(root, frozenset({".txt"}))
        names = {p.name for p in result}
        assert names == {"legit.txt"}

    def test_symlink_inside_root_is_kept(self, tmp_path: Path):
        """Symlinks that resolve inside the registered root are still ingested."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "real.txt").write_text("content")
        (root / "link.txt").symlink_to(root / "real.txt")

        result = discover_files(root, frozenset({".txt"}))
        names = {p.name for p in result}
        assert names == {"real.txt", "link.txt"}

    def test_broken_symlink_is_dropped(self, tmp_path: Path):
        """A symlink whose target does not exist is skipped without crashing."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "real.txt").write_text("content")
        (root / "broken.txt").symlink_to(tmp_path / "does-not-exist")

        result = discover_files(root, frozenset({".txt"}))
        names = {p.name for p in result}
        assert names == {"real.txt"}

    def test_nested_gitignore_respected(self, tmp_path: Path):
        """A .gitignore inside a subdirectory applies to that subtree."""
        project = tmp_path / "myproject"
        project.mkdir()
        (project / ".gitignore").write_text("data/\n*.log\n")
        data = project / "data"
        data.mkdir()
        (data / "big.csv").touch()
        (project / "debug.log").touch()
        (project / "app.py").touch()
        (tmp_path / "root.py").touch()
        result = discover_files(tmp_path, frozenset({".py", ".csv", ".log"}))
        names = sorted(p.name for p in result)
        assert names == ["app.py", "root.py"]

    def test_nested_gitignore_does_not_leak_to_siblings(self, tmp_path: Path):
        """Patterns in project-a/.gitignore don't affect project-b."""
        a = tmp_path / "project-a"
        b = tmp_path / "project-b"
        a.mkdir()
        b.mkdir()
        (a / ".gitignore").write_text("*.log\n")
        (a / "debug.log").touch()
        (b / "debug.log").touch()
        (a / "app.py").touch()
        (b / "app.py").touch()
        result = discover_files(tmp_path, frozenset({".py", ".log"}))
        names = sorted(p.name for p in result)
        # project-a/debug.log ignored, project-b/debug.log kept
        assert names == ["app.py", "app.py", "debug.log"]


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

    def _seed_with_hash(
        self,
        conn: sqlite3.Connection,
        f: Path,
        *,
        content_hash: str | None,
    ) -> None:
        """Insert a FileRecord for *f* matching disk state, with *content_hash*."""
        stat = f.stat()
        upsert_file(
            conn,
            FileRecord(
                path=str(f.resolve()),
                collection="col",
                document_name=f.name,
                mtime=stat.st_mtime,
                size=stat.st_size,
                ingested_at="2025-01-01",
                content_hash=content_hash,
            ),
        )

    def test_compute_sync_plan_refreshes_on_touch_without_content_change(
        self, tmp_path: Path
    ):
        conn, d = self._setup(tmp_path)
        f = d / "same.txt"
        f.write_bytes(b"stable content")
        self._seed_with_hash(conn, f, content_hash=_content_hash(f))

        # Bump mtime via os.utime; content byte-identical.
        stat = f.stat()
        os.utime(f, (stat.st_atime, stat.st_mtime + 100))

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert plan.to_ingest == []
        assert len(plan.to_refresh) == 1
        assert plan.to_refresh[0][0].name == "same.txt"
        assert plan.to_refresh[0][1] == _content_hash(f)
        assert plan.unchanged == 0
        conn.close()

    def test_compute_sync_plan_reingests_on_content_change_same_size(
        self, tmp_path: Path
    ):
        conn, d = self._setup(tmp_path)
        f = d / "edit.txt"
        f.write_bytes(b"aaaaa")
        self._seed_with_hash(conn, f, content_hash=_content_hash(f))

        # Replace content with the same length so only the hash differs.
        f.write_bytes(b"bbbbb")
        stat = f.stat()
        os.utime(f, (stat.st_atime, stat.st_mtime + 10))

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "edit.txt"
        assert plan.to_refresh == []
        conn.close()

    def test_compute_sync_plan_reingests_on_size_change(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        f = d / "grow.txt"
        f.write_bytes(b"short")
        self._seed_with_hash(conn, f, content_hash=_content_hash(f))

        with f.open("ab") as fh:
            fh.write(b"-longer-now")

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "grow.txt"
        assert plan.to_refresh == []
        conn.close()

    def test_compute_sync_plan_reingests_when_hash_missing(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        f = d / "legacy.txt"
        f.write_bytes(b"pre-migration row")
        self._seed_with_hash(conn, f, content_hash=None)

        stat = f.stat()
        os.utime(f, (stat.st_atime, stat.st_mtime + 10))

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "legacy.txt"
        assert plan.to_refresh == []
        conn.close()

    def test_compute_sync_plan_reingests_on_hash_read_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        conn, d = self._setup(tmp_path)
        f = d / "sadfile.txt"
        f.write_bytes(b"payload")
        self._seed_with_hash(conn, f, content_hash="cafebabe")

        stat = f.stat()
        os.utime(f, (stat.st_atime, stat.st_mtime + 10))

        def _boom(_path: Path) -> str:
            raise OSError("permission denied")

        monkeypatch.setattr("quarry.sync._content_hash", _boom)

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "sadfile.txt"
        assert plan.to_refresh == []
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

        with (
            patch("quarry.sync.prepare_document", side_effect=_fake_prepare),
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=1) as mock_batch,
        ):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)

        assert result.ingested == 1
        assert result.failed == 0
        assert result.skipped == 0
        mock_batch.assert_called_once()
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

        def side_effect(
            fp: Path, settings: object, **kwargs: object
        ) -> tuple[list[Chunk], np.ndarray]:
            if fp.name == "bad.txt":
                msg = "boom"
                raise RuntimeError(msg)
            return _fake_prepare(fp, settings, **kwargs)

        with (
            patch("quarry.sync.prepare_document", side_effect=side_effect),
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=0),
        ):
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
            patch("quarry.sync.prepare_document", side_effect=_fake_prepare),
            patch("quarry.sync.delete_document") as mock_del,
            patch("quarry.sync.batch_insert_chunks", return_value=0),
        ):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)

        assert result.deleted == 1
        # delete_document called for both the "gone" file in _delete_documents
        # and potentially for overwrite in _ingest_files.  Check at least the
        # deletion path call is present.
        del_calls = [c for c in mock_del.call_args_list if c.args[1] == "gone.txt"]
        assert len(del_calls) == 1
        conn.close()

    def test_registry_updated_after_sync(self, tmp_path: Path):
        conn, d = self._setup(tmp_path)
        (d / "new.txt").write_text("data")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        with (
            patch("quarry.sync.prepare_document", side_effect=_fake_prepare),
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=1),
        ):
            sync_collection(d, "col", db, settings, conn, max_workers=1)

        files = list_files(conn, "col")
        assert len(files) == 1
        assert files[0].document_name == "new.txt"
        conn.close()

    def test_subdirectory_document_names_match_registry(self, tmp_path: Path):
        """Regression: sync must use the same document_name in LanceDB and SQLite.

        When syncing a directory with subdirectories, the document_name for
        each file is a relative path (e.g. "sub/file.txt").  Both the
        prepare call and the registry upsert must use this relative name.

        See quarry-5sg.
        """
        conn, d = self._setup(tmp_path)
        sub = d / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("def hello():\n    pass\n")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        with (
            patch(
                "quarry.sync.prepare_document", side_effect=_fake_prepare
            ) as mock_prepare,
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=1),
        ):
            sync_collection(d, "col", db, settings, conn, max_workers=1)

        # The document_name passed to prepare_document must be the relative path
        _, kwargs = mock_prepare.call_args
        assert kwargs["document_name"] == "pkg/mod.py"

        # The registry must store the same relative path
        files = list_files(conn, "col")
        assert len(files) == 1
        assert files[0].document_name == "pkg/mod.py"
        conn.close()


class TestSyncCollectionDurabilityAndRefresh:
    """Tests for quarry-272m: content-hash refresh path and durable ingest."""

    def _setup(self, tmp_path: Path) -> tuple[sqlite3.Connection, Path, Path]:
        registry_path = tmp_path / "r.db"
        conn = open_registry(registry_path)
        d = tmp_path / "docs"
        d.mkdir()
        register_directory(conn, d, "col")
        return conn, d, registry_path

    def test_sync_collection_crash_before_batch_insert_leaves_no_registry_rows(
        self, tmp_path: Path
    ):
        """A crash mid-ingest must leave zero registry rows.

        Registry rows are written with ``commit=False`` during
        ``_ingest_files``; the commit happens only after
        ``batch_insert_chunks`` succeeds.  A crash before the batch
        insert rolls back all uncommitted registry rows so the next sync
        re-processes them — preventing silent data loss where the
        registry says a file is synced but LanceDB has no chunks.
        """
        conn, d, registry_path = self._setup(tmp_path)
        for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
            (d / name).write_text(f"content of {name}")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        calls: list[str] = []

        def _prepare(
            fp: Path,
            settings_arg: object,
            **kwargs: object,
        ) -> tuple[list[Chunk], np.ndarray]:
            name = kwargs["document_name"]
            assert isinstance(name, str)
            calls.append(name)
            if len(calls) == 3:
                msg = "user interrupt"
                raise KeyboardInterrupt(msg)
            return _fake_prepare(fp, settings_arg, **kwargs)

        with (
            patch("quarry.sync.prepare_document", side_effect=_prepare),
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=0),
            pytest.raises(KeyboardInterrupt),
        ):
            sync_collection(d, "col", db, settings, conn, max_workers=1)

        conn.close()

        # Fresh connection — must not rely on the sync's own conn's
        # uncommitted state.
        verify = sqlite3.connect(str(registry_path))
        verify.row_factory = sqlite3.Row
        rows = verify.execute(
            "SELECT path, content_hash FROM files WHERE collection = ? ORDER BY path",
            ("col",),
        ).fetchall()
        verify.close()

        # No rows committed because the crash happened before
        # batch_insert_chunks and the deferred conn.commit().
        assert len(rows) == 0, (
            f"expected 0 durable rows (deferred commit), got {len(rows)}; calls={calls}"
        )

    def test_sync_collection_refresh_path_updates_registry_mtime_without_reingest(
        self, tmp_path: Path
    ):
        """Bumping mtime on a hashed file must refresh, not re-ingest."""
        conn, d, _ = self._setup(tmp_path)
        for name in ("a.txt", "b.txt", "c.txt"):
            (d / name).write_text(f"payload for {name}")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        with (
            patch(
                "quarry.sync.prepare_document", side_effect=_fake_prepare
            ) as mock_prepare,
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=1),
        ):
            first = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert first.ingested == 3
        assert first.refreshed == 0
        assert mock_prepare.call_count == 3

        # Capture post-ingest mtimes from the registry.
        pre_refresh = {r.path: r.mtime for r in list_files(conn, "col")}

        # Bump every file's mtime without touching content.
        for name in ("a.txt", "b.txt", "c.txt"):
            f = d / name
            stat = f.stat()
            os.utime(f, (stat.st_atime, stat.st_mtime + 100))

        with (
            patch(
                "quarry.sync.prepare_document", side_effect=_fake_prepare
            ) as mock_prepare,
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=0),
        ):
            second = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert second.ingested == 0
        assert second.refreshed == 3
        assert second.skipped == 0
        assert mock_prepare.call_count == 0

        post_refresh = {r.path: r.mtime for r in list_files(conn, "col")}
        assert set(pre_refresh) == set(post_refresh)
        for path, old_mtime in pre_refresh.items():
            assert post_refresh[path] > old_mtime, (
                f"{path} mtime did not advance: {old_mtime} -> {post_refresh[path]}"
            )
        conn.close()

    def test_refresh_files_partial_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Class 2 failure-injection: _refresh_files catches OSError per file."""
        conn, d, registry_path = self._setup(tmp_path)
        for name in ("a.txt", "b.txt"):
            (d / name).write_text(f"content of {name}")

        db = MagicMock()
        settings = _mock_settings(tmp_path)

        # Initial ingest so content_hash is populated.
        with (
            patch("quarry.sync.prepare_document", side_effect=_fake_prepare),
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=1),
        ):
            first = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert first.ingested == 2

        pre_mtimes = {r.path: r.mtime for r in list_files(conn, "col")}

        # Bump mtimes without changing content — triggers refresh path.
        for name in ("a.txt", "b.txt"):
            f = d / name
            stat = f.stat()
            os.utime(f, (stat.st_atime, stat.st_mtime + 200))

        # Make upsert_file raise OSError on the second call only.
        call_count = 0
        _real_upsert = upsert_file

        def _failing_upsert(
            conn_arg: sqlite3.Connection,
            record: FileRecord,
            *,
            commit: bool = True,
        ) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("disk full")
            _real_upsert(conn_arg, record, commit=commit)

        monkeypatch.setattr("quarry.sync.upsert_file", _failing_upsert)

        with (
            patch("quarry.sync.prepare_document", side_effect=_fake_prepare),
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=0),
        ):
            second = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert second.refreshed == 1
        assert second.failed == 1
        assert len(second.errors) == 1
        assert "disk full" in second.errors[0]

        # Verify via a fresh connection: only the first file's mtime advanced.
        verify = open_registry(registry_path)
        post_mtimes = {r.path: r.mtime for r in list_files(verify, "col")}
        verify.close()

        updated = [p for p, m in post_mtimes.items() if m != pre_mtimes[p]]
        assert len(updated) == 1, (
            f"expected exactly 1 file refreshed, got {len(updated)}"
        )
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

        with (
            patch("quarry.sync.prepare_document", side_effect=_fake_prepare),
            patch("quarry.sync.delete_document"),
            patch("quarry.sync.batch_insert_chunks", return_value=1),
        ):
            results = sync_all(db, settings, max_workers=1)

        assert "alpha" in results
        assert "beta" in results
        assert results["alpha"].ingested == 1
        assert results["beta"].ingested == 1
