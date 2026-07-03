from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import numpy as np
import pytest

from quarry.config import Settings
from quarry.db.chunk_store import ChunkStore
from quarry.db.chunk_table import ChunkTable
from quarry.db.optimizer import TableOptimizer
from quarry.db.schema import TABLE_NAME
from quarry.db.storage import get_db
from quarry.ingestion.pipeline import plan_file_chunks
from quarry.ingestion.progressive import FlushCheckpoint, ProgressiveIndexer
from quarry.models import PageContent, PageType
from quarry.sync import compute_sync_plan, sync_all, sync_collection
from quarry.sync_discovery import _DEFAULT_IGNORE_PATTERNS, FileDiscovery
from quarry.sync_ingest import CollectionIngestor, _FileMeta
from quarry.sync_registry import FileRecord, SyncRegistry
from quarry.sync_resume import HASH_UNKNOWN, ResumePolicy

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from quarry.models import Chunk
    from quarry.types import LanceDB


def _run_with_timeout(fn: Callable[[], object], *, timeout: float = 20.0) -> object:
    """Run *fn* in a thread; fail fast if it does not finish (deadlock guard).

    Producer/consumer liveness regressions manifest as a hang, not a wrong value;
    running under a watchdog turns a would-be hang into a clear failure.
    """
    box: dict[str, object] = {}

    def target() -> None:
        box["value"] = fn()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        pytest.fail(f"sync did not terminate within {timeout}s — deadlock regression")
    return box.get("value")


class _FakeEmbedder:
    """Deterministic embedder: records embedded texts, returns zero vectors."""

    def __init__(self) -> None:
        self.embedded: list[str] = []

    @property
    def dimension(self) -> int:
        return 768

    @property
    def model_name(self) -> str:
        return "fake"

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        self.embedded.extend(texts)
        return np.zeros((len(texts), 768), dtype=np.float32)

    def embed_query(self, query: str) -> NDArray[np.float32]:
        return np.zeros(768, dtype=np.float32)


class _RaisingEmbedder(_FakeEmbedder):
    """Embeds normally until the *fail_on_call*-th embed_texts, then raises."""

    def __init__(self, fail_on_call: int) -> None:
        super().__init__()
        self._fail_on_call = fail_on_call
        self._calls = 0

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        self._calls += 1
        if self._calls >= self._fail_on_call:
            msg = "embedder boom"
            raise RuntimeError(msg)
        return super().embed_texts(texts)


class _FakeOcr:
    """OCR backend double that returns a fixed IMAGE page (non-deterministic)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def _page(self, document_name: str, document_path: Path) -> PageContent:
        return PageContent(
            document_name=document_name,
            document_path=str(document_path),
            page_number=1,
            total_pages=1,
            text=self._text,
            page_type=PageType.IMAGE,
        )

    def ocr_image_bytes(
        self, image_bytes: bytes, document_name: str, document_path: Path
    ) -> PageContent:
        return self._page(document_name, document_path)

    def ocr_document(
        self,
        document_path: Path,
        page_numbers: list[int],
        total_pages: int,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        return [self._page(document_name or "", document_path)]


def _settings(
    tmp_path: Path,
    *,
    flush_mb: int = 32,
    window: int = 512,
    max_chars: int = 1800,
    overlap: int = 0,
) -> Settings:
    return Settings(
        quarry_root=tmp_path / "data",
        lancedb_path=tmp_path / "lancedb",
        registry_path=tmp_path / "registry.db",
        chunk_max_chars=max_chars,
        chunk_overlap_chars=overlap,
        sync_flush_mb=flush_mb,
        embed_window_chunks=window,
    )


@contextmanager
def _patched_embedder(embedder: _FakeEmbedder) -> Iterator[_FakeEmbedder]:
    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=embedder
    ):
        yield embedder


def _chunk_indexes(db: LanceDB, document_name: str) -> list[int]:
    table = db.open_table(TABLE_NAME)
    rows = (
        table.search()
        .where(f"document_name = '{document_name}'")
        .select(["chunk_index"])
        .limit(100_000)
        .to_list()
    )
    return sorted(cast("int", r["chunk_index"]) for r in rows)


class TestDiscoverFiles:
    def test_finds_supported_files(self, tmp_path: Path):
        (tmp_path / "a.pdf").touch()
        (tmp_path / "b.txt").touch()
        (tmp_path / "c.xyz").touch()
        exts = frozenset({".pdf", ".txt"})
        result = FileDiscovery(tmp_path).discover(exts)
        names = [p.name for p in result]
        assert "a.pdf" in names
        assert "b.txt" in names
        assert "c.xyz" not in names

    def test_recursive_discovery(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.pdf").touch()
        exts = frozenset({".pdf"})
        result = FileDiscovery(tmp_path).discover(exts)
        assert len(result) == 1
        assert result[0].name == "deep.pdf"

    def test_ignores_unsupported(self, tmp_path: Path):
        (tmp_path / "notes.log").touch()
        (tmp_path / "data.csv").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".pdf"}))
        assert result == []

    def test_empty_directory(self, tmp_path: Path):
        result = FileDiscovery(tmp_path).discover(frozenset({".pdf", ".txt"}))
        assert result == []

    def test_returns_sorted_absolute_paths(self, tmp_path: Path):
        (tmp_path / "z.pdf").touch()
        (tmp_path / "a.pdf").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".pdf"}))
        assert len(result) == 2
        assert result[0].name == "a.pdf"
        assert result[1].name == "z.pdf"
        assert all(p.is_absolute() for p in result)

    def test_skips_resource_fork_files(self, tmp_path: Path):
        (tmp_path / "report.pdf").touch()
        (tmp_path / "._report.pdf").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".pdf"}))
        assert len(result) == 1
        assert result[0].name == "report.pdf"

    def test_skips_trash_directory(self, tmp_path: Path):
        trash = tmp_path / ".Trash"
        trash.mkdir()
        (trash / "deleted.pdf").touch()
        (tmp_path / "keep.pdf").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".pdf"}))
        assert len(result) == 1
        assert result[0].name == "keep.pdf"

    def test_skips_dotfiles_in_subdirs(self, tmp_path: Path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "._hidden.pdf").touch()
        (sub / "visible.pdf").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".pdf"}))
        assert len(result) == 1
        assert result[0].name == "visible.pdf"

    def test_skips_files_in_hidden_directories(self, tmp_path: Path):
        hidden = tmp_path / ".git"
        hidden.mkdir()
        (hidden / "config.txt").touch()
        (tmp_path / "notes.txt").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".txt"}))
        assert len(result) == 1
        assert result[0].name == "notes.txt"

    def test_skips_venv_by_default(self, tmp_path: Path):
        venv = tmp_path / "venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "module.py").touch()
        (tmp_path / "app.py").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".py"}))
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
        result = FileDiscovery(tmp_path).discover(frozenset({".txt"}))
        assert len(result) == 1
        assert result[0].name == "readme.txt"

    def test_skips_pycache_by_default(self, tmp_path: Path):
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "module.cpython-313.pyc").touch()
        (tmp_path / "module.py").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".py", ".pyc"}))
        assert len(result) == 1
        assert result[0].name == "module.py"

    def test_respects_gitignore(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("data/\n*.log\n")
        data = tmp_path / "data"
        data.mkdir()
        (data / "big.csv").touch()
        (tmp_path / "debug.log").touch()
        (tmp_path / "app.txt").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".csv", ".log", ".txt"}))
        assert len(result) == 1
        assert result[0].name == "app.txt"

    def test_respects_quarryignore(self, tmp_path: Path):
        (tmp_path / ".quarryignore").write_text("archive/\n")
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "old.pdf").touch()
        (tmp_path / "new.pdf").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".pdf"}))
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
        result = FileDiscovery(tmp_path).discover(frozenset({".txt", ".log"}))
        assert len(result) == 1
        assert result[0].name == "app.txt"

    def test_gitignore_negation(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("*.txt\n!important.txt\n")
        (tmp_path / "notes.txt").touch()
        (tmp_path / "important.txt").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".txt"}))
        assert len(result) == 1
        assert result[0].name == "important.txt"

    def test_deeply_nested_venv_skipped(self, tmp_path: Path):
        deep = tmp_path / "venv" / "lib" / "python3.13" / "site-packages" / "numpy"
        deep.mkdir(parents=True)
        (deep / "core.py").touch()
        (tmp_path / "main.py").touch()
        result = FileDiscovery(tmp_path).discover(frozenset({".py"}))
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

        result = FileDiscovery(root).discover(frozenset({".txt"}))
        names = {p.name for p in result}
        assert names == {"legit.txt"}

    def test_symlink_inside_root_is_kept(self, tmp_path: Path):
        """Symlinks that resolve inside the registered root are still ingested."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "real.txt").write_text("content")
        (root / "link.txt").symlink_to(root / "real.txt")

        result = FileDiscovery(root).discover(frozenset({".txt"}))
        names = {p.name for p in result}
        assert names == {"real.txt", "link.txt"}

    def test_broken_symlink_is_dropped(self, tmp_path: Path):
        """A symlink whose target does not exist is skipped without crashing."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "real.txt").write_text("content")
        (root / "broken.txt").symlink_to(tmp_path / "does-not-exist")

        result = FileDiscovery(root).discover(frozenset({".txt"}))
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
        result = FileDiscovery(tmp_path).discover(frozenset({".py", ".csv", ".log"}))
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
        result = FileDiscovery(tmp_path).discover(frozenset({".py", ".log"}))
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
        spec = FileDiscovery(tmp_path).load_ignore_spec()
        assert spec.match_file("debug.log")
        assert spec.match_file("output/")
        assert not spec.match_file("app.py")

    def test_loads_quarryignore(self, tmp_path: Path):
        (tmp_path / ".quarryignore").write_text("scratch/\n")
        spec = FileDiscovery(tmp_path).load_ignore_spec()
        assert spec.match_file("scratch/")

    def test_no_ignore_files_uses_defaults(self, tmp_path: Path):
        spec = FileDiscovery(tmp_path).load_ignore_spec()
        assert spec.match_file("venv/")
        assert spec.match_file("node_modules/")
        assert not spec.match_file("src/app.py")

    def test_comments_and_blanks_ignored(self, tmp_path: Path):
        (tmp_path / ".gitignore").write_text("# comment\n\n*.log\n")
        spec = FileDiscovery(tmp_path).load_ignore_spec()
        assert spec.match_file("debug.log")
        assert not spec.match_file("# comment")


class TestComputeSyncPlan:
    EXTS = frozenset({".pdf", ".txt"})

    def _setup(self, tmp_path: Path) -> tuple[SyncRegistry, Path]:
        """Create registry, docs directory, and register collection 'col'."""
        conn = SyncRegistry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()
        conn.register_directory(d, "col")
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
        conn.upsert_file(
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
        conn.upsert_file(
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
        conn.upsert_file(
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
        conn.upsert_file(
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
        conn.upsert_file(
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
        conn: SyncRegistry,
        f: Path,
        *,
        content_hash: str | None,
    ) -> None:
        """Insert a FileRecord for *f* matching disk state, with *content_hash*."""
        stat = f.stat()
        conn.upsert_file(
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
        self._seed_with_hash(conn, f, content_hash=FileDiscovery.content_hash(f))

        # Bump mtime via os.utime; content byte-identical.
        stat = f.stat()
        os.utime(f, (stat.st_atime, stat.st_mtime + 100))

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert plan.to_ingest == []
        assert len(plan.to_refresh) == 1
        assert plan.to_refresh[0][0].name == "same.txt"
        assert plan.to_refresh[0][1] == FileDiscovery.content_hash(f)
        assert plan.unchanged == 0
        conn.close()

    def test_compute_sync_plan_reingests_on_content_change_same_size(
        self, tmp_path: Path
    ):
        conn, d = self._setup(tmp_path)
        f = d / "edit.txt"
        f.write_bytes(b"aaaaa")
        self._seed_with_hash(conn, f, content_hash=FileDiscovery.content_hash(f))

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
        self._seed_with_hash(conn, f, content_hash=FileDiscovery.content_hash(f))

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

        monkeypatch.setattr(
            "quarry.sync.FileDiscovery.content_hash", staticmethod(_boom)
        )

        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "sadfile.txt"
        assert plan.to_refresh == []
        conn.close()

    def test_partial_watermark_routes_to_ingest(self, tmp_path: Path):
        """A row with a partial resume watermark always re-enters to_ingest."""
        conn, d = self._setup(tmp_path)
        f = d / "resume.txt"
        f.write_bytes(b"stable content")
        stat = f.stat()
        conn.upsert_file(
            FileRecord(
                path=str(f.resolve()),
                collection="col",
                document_name="resume.txt",
                mtime=stat.st_mtime,
                size=stat.st_size,
                ingested_at="2025-01-01",
                content_hash="h",
                chunks_committed=3,
                partial_hash="h",  # mid-file — must resume
            ),
        )
        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert [p.name for p in plan.to_ingest] == ["resume.txt"]
        assert plan.unchanged == 0
        conn.close()


# ---------------------------------------------------------------------------
# Progressive sync against a real LanceDB with a deterministic fake embedder
# ---------------------------------------------------------------------------

_SENTENCE = "The quick brown fox jumps over the lazy dog. "


def _make_collection(
    tmp_path: Path, settings: Settings
) -> tuple[LanceDB, SyncRegistry, Path]:
    d = tmp_path / "docs"
    d.mkdir()
    db = get_db(settings.lancedb_path)
    conn = SyncRegistry(settings.registry_path)
    conn.register_directory(d, "col")
    return db, conn, d


def _seed_crash_state(
    tmp_path: Path,
    *,
    watermark_from_total: float,
    prefill_from_total: float,
    partial_hash: str | None = None,
    use_real_hash: bool = True,
) -> tuple[Settings, LanceDB, SyncRegistry, Path, str, list[Chunk], int, int]:
    """Set up a post-crash state: chunks [0, prefill) durable, watermark at *w*.

    ``watermark_from_total`` / ``prefill_from_total`` are fractions of the file's
    chunk count so tests read as "resume from the middle" independent of chunking.
    """
    settings = _settings(tmp_path, max_chars=45)
    db, conn, d = _make_collection(tmp_path, settings)
    f = d / "big.txt"
    f.write_text(_SENTENCE * 12)
    doc = "big.txt"
    chunks, _ = plan_file_chunks(f, settings, collection="col", document_name=doc)
    total = len(chunks)
    assert total >= 4
    w = int(total * watermark_from_total)
    prefill = int(total * prefill_from_total)
    if prefill:
        ChunkStore(db).insert(
            chunks[:prefill], np.zeros((prefill, 768), dtype=np.float32)
        )
    real_hash = FileDiscovery.content_hash(f)
    stored = real_hash if use_real_hash else partial_hash
    conn.upsert_file(
        FileRecord(
            path=str(f.resolve()),
            collection="col",
            document_name=doc,
            mtime=f.stat().st_mtime,
            size=f.stat().st_size,
            ingested_at="2025-01-01",
            content_hash=stored,
            chunks_committed=w,
            partial_hash=stored,
        ),
    )
    return settings, db, conn, d, doc, chunks, total, w


class TestSyncCollectionProgressive:
    def test_ingests_new_file(self, tmp_path: Path):
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        (d / "a.txt").write_text(_SENTENCE * 3)
        with _patched_embedder(_FakeEmbedder()):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert result.ingested == 1
        assert result.failed == 0
        assert ChunkStore(db).count(collection_filter="col") >= 1
        files = conn.list_files("col")
        assert len(files) == 1
        assert files[0].partial_hash is None  # complete
        assert files[0].content_hash is not None
        conn.close()

    def test_error_isolation(self, tmp_path: Path):
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        (d / "good.txt").write_text(_SENTENCE * 2)
        (d / "bad.txt").write_text(_SENTENCE * 2)

        real_plan = plan_file_chunks

        def flaky(fp: Path, *a: object, **k: object) -> object:
            if fp.name == "bad.txt":
                msg = "boom"
                raise RuntimeError(msg)
            return real_plan(fp, *a, **k)  # type: ignore[arg-type]

        with (
            _patched_embedder(_FakeEmbedder()),
            patch("quarry.sync_ingest.plan_file_chunks", side_effect=flaky),
        ):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert result.ingested == 1
        assert result.failed == 1
        assert any("bad.txt" in e for e in result.errors)
        conn.close()

    def test_deletes_removed_files(self, tmp_path: Path):
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        conn.upsert_file(
            FileRecord(
                path=str((d / "gone.txt").resolve()),
                collection="col",
                document_name="gone.txt",
                mtime=100.0,
                size=50,
                ingested_at="2025-01-01",
            ),
        )
        with _patched_embedder(_FakeEmbedder()):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert result.deleted == 1
        assert conn.get_file(str((d / "gone.txt").resolve())) is None
        conn.close()

    def test_idempotent_reingest(self, tmp_path: Path):
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        (d / "a.txt").write_text(_SENTENCE * 3)
        with _patched_embedder(_FakeEmbedder()):
            first = sync_collection(d, "col", db, settings, conn, max_workers=1)
            count_after_first = ChunkStore(db).count(collection_filter="col")
            second = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert first.ingested == 1
        assert second.ingested == 0
        assert second.skipped == 1
        assert ChunkStore(db).count(collection_filter="col") == count_after_first
        conn.close()


class TestWithinFileResume:
    def test_happy_resume_embeds_only_tail(self, tmp_path: Path):
        """G1: resume embeds only [w, total); final set is contiguous, no gaps."""
        settings, db, conn, d, doc, chunks, total, w = _seed_crash_state(
            tmp_path, watermark_from_total=0.5, prefill_from_total=0.5
        )
        embedder = _FakeEmbedder()
        with _patched_embedder(embedder):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert result.ingested == 1
        assert embedder.embedded == [c.text for c in chunks[w:]]  # tail only
        assert _chunk_indexes(db, doc) == list(range(total))
        rec = conn.get_file(str((d / "big.txt").resolve()))
        assert rec is not None and rec.partial_hash is None
        conn.close()

    def test_g2_delete_tail_dedups(self, tmp_path: Path):
        """G2: durable [w, K) with unadvanced watermark is delete-tailed, no dups."""
        # prefill past the watermark: a crash left extra durable chunks.
        settings, db, conn, d, doc, chunks, total, w = _seed_crash_state(
            tmp_path, watermark_from_total=0.33, prefill_from_total=0.9
        )
        embedder = _FakeEmbedder()
        with _patched_embedder(embedder):
            sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert _chunk_indexes(db, doc) == list(range(total))  # no [w, K) dups
        assert embedder.embedded == [c.text for c in chunks[w:]]
        conn.close()

    def test_g3_hash_mismatch_full_reembed(self, tmp_path: Path):
        """G3: partial_hash != content_hash discards the watermark, re-embeds all."""
        settings, db, conn, d, doc, chunks, total, _w = _seed_crash_state(
            tmp_path,
            watermark_from_total=0.5,
            prefill_from_total=0.5,
            partial_hash="STALE",
            use_real_hash=False,
        )
        embedder = _FakeEmbedder()
        with _patched_embedder(embedder):
            sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert embedder.embedded == [c.text for c in chunks]  # full re-embed
        assert _chunk_indexes(db, doc) == list(range(total))
        conn.close()

    def test_g3_ocr_image_extraction_full_reembed(self, tmp_path: Path):
        """G3: an OCR'd (IMAGE) extraction is non-deterministic → discard watermark.

        Drives the real image path (``_extract_image_pages`` → OCR backend) rather
        than patching the determinism flag, so the classifier itself is exercised.
        """
        from PIL import Image

        settings = _settings(tmp_path, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        img = d / "scan.png"
        Image.new("RGB", (64, 64), "white").save(img)
        ocr = _FakeOcr(_SENTENCE * 12)

        with patch("quarry.ingestion.pipeline.get_ocr_backend", return_value=ocr):
            chunks, deterministic = plan_file_chunks(
                img, settings, collection="col", document_name="scan.png"
            )
        assert deterministic is False  # OCR pages are IMAGE → non-deterministic
        total = len(chunks)
        assert total >= 4
        w = total // 2
        content_hash = FileDiscovery.content_hash(img)
        ChunkStore(db).insert(chunks[:w], np.zeros((w, 768), dtype=np.float32))
        conn.upsert_file(
            FileRecord(
                path=str(img.resolve()),
                collection="col",
                document_name="scan.png",
                mtime=img.stat().st_mtime,
                size=img.stat().st_size,
                ingested_at="2025-01-01",
                content_hash=content_hash,  # hash matches — only OCR non-determinism
                chunks_committed=w,
                partial_hash=content_hash,
            ),
        )
        embedder = _FakeEmbedder()
        with (
            patch("quarry.ingestion.pipeline.get_ocr_backend", return_value=ocr),
            _patched_embedder(embedder),
        ):
            sync_collection(d, "col", db, settings, conn, max_workers=1)
        # Non-deterministic extraction → watermark discarded → full re-embed from 0.
        assert embedder.embedded == [c.text for c in chunks]
        assert _chunk_indexes(db, "scan.png") == list(range(total))
        conn.close()


class TestPartialHashSentinel:
    """Fix #327-1: a mid-file flush with an unknown content hash must stay partial."""

    def test_partial_mark_uses_sentinel_when_hash_none(self):
        """An incomplete checkpoint with no content hash marks the row partial."""
        policy = ResumePolicy()
        incomplete = FlushCheckpoint(file_id="f", chunks_committed=3, complete=False)
        assert policy.partial_mark(incomplete, None) == HASH_UNKNOWN
        assert policy.partial_mark(incomplete, "abc") == "abc"

    def test_partial_mark_clears_on_complete(self):
        """A complete checkpoint clears the mark regardless of the hash."""
        policy = ResumePolicy()
        complete = FlushCheckpoint(file_id="f", chunks_committed=9, complete=True)
        assert policy.partial_mark(complete, None) is None
        assert policy.partial_mark(complete, "abc") is None

    def test_resume_gate_never_trusts_unknown_hash(self):
        """A hash-unknown watermark always re-embeds from 0, even if in range."""
        policy = ResumePolicy()
        record = FileRecord(
            path="/p",
            collection="col",
            document_name="d",
            mtime=1.0,
            size=1,
            ingested_at="2025-01-01",
            content_hash="real",
            chunks_committed=5,
            partial_hash=HASH_UNKNOWN,
        )
        watermark = policy.resume_watermark(
            record, "real", total=10, deterministic=True
        )
        assert watermark == 0

    def test_incomplete_flush_no_hash_persists_partial_row(self, tmp_path: Path):
        """on_flush persists is_partial=True when content_hash is None (the bug)."""
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        f = d / "big.txt"
        f.write_text(_SENTENCE * 4)
        file_id = str(f.resolve())
        ingestor = CollectionIngestor(
            ChunkStore(db),
            conn,
            settings,
            collection="col",
            resolved=d.resolve(),
            max_workers=1,
            progress=lambda _m: None,
        )
        record = FileRecord(
            path=file_id,
            collection="col",
            document_name="big.txt",
            mtime=f.stat().st_mtime,
            size=f.stat().st_size,
            ingested_at="2025-01-01",
            content_hash=None,  # hashing failed
        )
        ingestor._meta[file_id] = _FileMeta(
            record=record, resume_watermark=0, total_chunks=10
        )
        ingestor.on_flush(
            [FlushCheckpoint(file_id=file_id, chunks_committed=3, complete=False)]
        )
        rec = conn.get_file(file_id)
        assert rec is not None
        assert rec.is_partial is True  # before the fix this was False (silent skip)
        assert rec.chunks_committed == 3
        conn.close()

    def test_unknown_hash_watermark_reingests_full(self, tmp_path: Path):
        """Next sync of a hash-unknown partial row re-embeds all chunks, no skip."""
        settings = _settings(tmp_path, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        f = d / "big.txt"
        f.write_text(_SENTENCE * 12)
        doc = "big.txt"
        chunks, _ = plan_file_chunks(f, settings, collection="col", document_name=doc)
        total = len(chunks)
        assert total >= 4
        w = total // 2
        ChunkStore(db).insert(chunks[:w], np.zeros((w, 768), dtype=np.float32))
        conn.upsert_file(
            FileRecord(
                path=str(f.resolve()),
                collection="col",
                document_name=doc,
                mtime=f.stat().st_mtime,
                size=f.stat().st_size,
                ingested_at="2025-01-01",
                content_hash=FileDiscovery.content_hash(f),
                chunks_committed=w,
                partial_hash=HASH_UNKNOWN,  # watermark whose hash is unknown
            ),
        )
        embedder = _FakeEmbedder()
        with _patched_embedder(embedder):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert result.ingested == 1
        assert embedder.embedded == [c.text for c in chunks]  # full re-embed from 0
        assert _chunk_indexes(db, doc) == list(range(total))
        rec = conn.get_file(str(f.resolve()))
        assert rec is not None and rec.partial_hash is None  # completed, mark cleared
        conn.close()


class TestFragmentBudgetAndExceptions:
    def test_risk1_many_tiny_files_coalesce(self, tmp_path: Path):
        """RISK-1: N tiny files share flushes — fragments O(vectors/N), not O(files)."""
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        for i in range(200):
            (d / f"f{i}.txt").write_text("tiny one sentence.")

        orig = ChunkStore.insert_records
        calls = {"n": 0}

        def counting(self: ChunkStore, records: list[dict[str, object]]) -> int:
            calls["n"] += 1
            return orig(self, records)

        with (
            _patched_embedder(_FakeEmbedder()),
            patch.object(ChunkStore, "insert_records", counting),
        ):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert result.ingested == 200
        # 200 tiny files coalesce into O(vectors/N) flushes (a handful), not O(200).
        # Fragment count therefore tracks flush count, not file count — the
        # DES-026 death-spiral door stays shut. (The threshold skip/run branch is
        # covered directly by test_sync_concurrency::TestOptimizeGuard; the
        # optimize() compaction effect by test_optimize_strictly_reduces_fragments.)
        assert calls["n"] <= 3
        assert TableOptimizer(db).count_fragments() == calls["n"]
        assert ChunkStore(db).count(collection_filter="col") == 200
        conn.close()

    def test_optimize_compacts_fragments_strictly_fewer(self, tmp_path: Path):
        """Compaction strictly reduces fragments: 4 separate adds → 1 compacted.

        ``count_fragments`` is a disk-file proxy and our ``optimize()`` retains
        superseded versions for 1 h (rollback safety, DES-023), so the decrease is
        observable only after those files are pruned. Forcing immediate cleanup
        exposes the compaction our optimize() performs.
        """
        from datetime import timedelta

        settings = _settings(tmp_path, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        f = d / "a.txt"
        f.write_text(_SENTENCE * 8)  # small chunks → several chunks to fragment
        chunks, _ = plan_file_chunks(f, settings, document_name="a.txt")
        assert len(chunks) >= 4
        store = ChunkStore(db)
        # Each insert_records is one table.add → one Lance fragment.
        for chunk in chunks[:4]:
            store.insert_records(
                ChunkTable.build_records([chunk], np.zeros((1, 768), dtype=np.float32))
            )
        opt = TableOptimizer(db)
        before = opt.count_fragments()
        assert before > 1
        db.open_table(TABLE_NAME).optimize(cleanup_older_than=timedelta(0))
        after = opt.count_fragments()
        assert after < before  # strictly fewer fragments after compaction
        assert store.count() == 4  # compaction is lossless
        conn.close()

    def test_commit_failure_reconciles_next_sync(self, tmp_path: Path):
        """conn.commit raising leaves durable chunks + no watermark; resume fixes it."""
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        f = d / "a.txt"
        f.write_text(_SENTENCE * 4)
        resolved = d.resolve()

        def boom() -> None:
            msg = "commit boom"
            raise RuntimeError(msg)

        ingestor = CollectionIngestor(
            ChunkStore(db),
            conn,
            settings,
            collection="col",
            resolved=resolved,
            max_workers=1,
            progress=lambda _m: None,
        )
        original_commit = conn.commit
        conn.commit = boom  # type: ignore[method-assign]
        with _patched_embedder(_FakeEmbedder()):
            _ingested, failed, errors = ingestor.run([resolved / "a.txt"])
        conn.commit = original_commit  # type: ignore[method-assign]
        assert failed >= 1
        assert errors
        # Chunks were written to Lance before the failing commit — durable.
        assert ChunkStore(db).count(collection_filter="col") >= 1
        # But the registry rolled back: no committed row for the file.
        assert conn.get_file(str(resolved / "a.txt")) is None

        # A clean sync reconciles via delete-tail: exact chunks, no duplicates.
        embedder = _FakeEmbedder()
        with _patched_embedder(embedder):
            CollectionIngestor(
                ChunkStore(db),
                conn,
                settings,
                collection="col",
                resolved=resolved,
                max_workers=1,
                progress=lambda _m: None,
            ).run([resolved / "a.txt"])
        chunks, _ = plan_file_chunks(
            f, settings, collection="col", document_name="a.txt"
        )
        assert _chunk_indexes(db, "a.txt") == list(range(len(chunks)))
        rec = conn.get_file(str(resolved / "a.txt"))
        assert rec is not None and rec.partial_hash is None
        conn.close()

    def test_embedder_raises_mid_file_leaves_partial_watermark(self, tmp_path: Path):
        """An embedder failure mid-file leaves flushed windows durable + a watermark."""
        settings = _settings(tmp_path, max_chars=45, window=2)
        db, conn, d = _make_collection(tmp_path, settings)
        f = d / "big.txt"
        f.write_text(_SENTENCE * 12)
        total = len(
            plan_file_chunks(f, settings, collection="col", document_name="big.txt")[0]
        )
        # Raise on the 2nd embed window so window 1 (chunks [0, 2)) is durable.
        embedder = _RaisingEmbedder(fail_on_call=2)
        with _patched_embedder(embedder):
            result = sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert result.failed == 1
        rec = conn.get_file(str(f.resolve()))
        assert rec is not None
        assert rec.is_partial is True  # a torn-free resume watermark was stored
        assert 0 < rec.chunks_committed < total
        # A clean resume completes with no duplicate chunk indexes.
        embedder2 = _FakeEmbedder()
        with _patched_embedder(embedder2):
            sync_collection(d, "col", db, settings, conn, max_workers=1)
        assert _chunk_indexes(db, "big.txt") == list(range(total))
        rec2 = conn.get_file(str(f.resolve()))
        assert rec2 is not None and rec2.partial_hash is None
        conn.close()


class _ConsumerBoomError(Exception):
    """A consumer-side error outside _RECOVERABLE/_FLUSH_ERRORS (e.g. MemoryError)."""


class TestConcurrencyLiveness:
    """Regressions for the producer/consumer deadlock blockers (#1, #2)."""

    def test_producer_non_recoverable_fails_cleanly_no_hang(self, tmp_path: Path):
        """A KeyError (not in _RECOVERABLE) in a producer must not hang the sync."""
        settings = _settings(tmp_path)
        db, conn, d = _make_collection(tmp_path, settings)
        (d / "good.txt").write_text(_SENTENCE * 2)
        (d / "bad.txt").write_text(_SENTENCE * 2)

        real_plan = plan_file_chunks

        def flaky(fp: Path, *a: object, **k: object) -> object:
            if fp.name == "bad.txt":
                raise KeyError("non-recoverable producer failure")
            return real_plan(fp, *a, **k)  # type: ignore[arg-type]

        with (
            _patched_embedder(_FakeEmbedder()),
            patch("quarry.sync_ingest.plan_file_chunks", side_effect=flaky),
        ):
            result = _run_with_timeout(
                lambda: sync_collection(d, "col", db, settings, conn, max_workers=2)
            )
        assert result is not None
        assert result.ingested == 1  # type: ignore[attr-defined]
        assert result.failed == 1  # type: ignore[attr-defined]
        assert any("bad.txt" in e for e in result.errors)  # type: ignore[attr-defined]
        conn.close()

    def test_consumer_non_flush_error_aborts_no_deadlock(self, tmp_path: Path):
        """A consumer flush error outside _FLUSH_ERRORS aborts + drains, no hang."""
        settings = _settings(tmp_path, flush_mb=1, window=8, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        # >341 chunks so a size-gated flush fires mid-file on the consumer thread.
        (d / "big.txt").write_text(_SENTENCE * 800)
        (d / "small.txt").write_text(_SENTENCE * 2)

        def boom(_self: ChunkStore, _records: list[dict[str, object]]) -> int:
            raise _ConsumerBoomError("table.add blew up")

        with (
            _patched_embedder(_FakeEmbedder()),
            patch.object(ChunkStore, "insert_records", boom),
        ):
            result = _run_with_timeout(
                lambda: sync_collection(d, "col", db, settings, conn, max_workers=2)
            )
        assert result is not None
        assert result.failed >= 1  # type: ignore[attr-defined]
        assert result.errors  # type: ignore[attr-defined]

        # Post-abort consistency: the aborted sync left nothing half-written, so a
        # clean re-sync (insert_records restored) yields contiguous, zero-dup chunks
        # and complete registry rows for both files.
        with _patched_embedder(_FakeEmbedder()):
            _run_with_timeout(
                lambda: sync_collection(d, "col", db, settings, conn, max_workers=2)
            )
        for name in ("big.txt", "small.txt"):
            total = len(plan_file_chunks(d / name, settings, document_name=name)[0])
            assert _chunk_indexes(db, name) == list(range(total))
        rows = {r.document_name: r for r in conn.list_files("col")}
        assert rows["big.txt"].partial_hash is None
        assert rows["small.txt"].partial_hash is None
        conn.close()

    def test_parallel_sync_two_files_max_workers_2(self, tmp_path: Path):
        """End-to-end sync under real concurrency (single-consumer serializes)."""
        settings = _settings(tmp_path, flush_mb=1, window=8, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        (d / "a.txt").write_text(_SENTENCE * 300)
        (d / "b.txt").write_text(_SENTENCE * 300)
        with _patched_embedder(_FakeEmbedder()):
            result = _run_with_timeout(
                lambda: sync_collection(d, "col", db, settings, conn, max_workers=2)
            )
        assert result is not None
        assert result.ingested == 2  # type: ignore[attr-defined]
        assert result.failed == 0  # type: ignore[attr-defined]
        # Each file's chunk indexes are contiguous [0, n) with no interleave gaps.
        a_chunks, _ = plan_file_chunks(d / "a.txt", settings, document_name="a.txt")
        assert _chunk_indexes(db, "a.txt") == list(range(len(a_chunks)))
        rows = {r.document_name: r for r in conn.list_files("col")}
        assert rows["a.txt"].partial_hash is None
        assert rows["b.txt"].partial_hash is None
        conn.close()

    def test_g4_two_file_flush_crash_reconciles_both(self, tmp_path: Path):
        """G4 end-to-end: one flush carries A-final + B-partial; commit fails there.

        A (small) completes and B (large) is mid-file in the SAME size-gated flush.
        conn.commit raises after the Lance add, so neither file's registry row
        commits (all-or-none). A clean re-sync reconciles both with zero dups.
        """
        settings = _settings(tmp_path, flush_mb=1, window=8, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        # ~170 chunks < one 1 MB flush (341), so A buffers whole; B spills the flush.
        (d / "a.txt").write_text(_SENTENCE * 170)
        (d / "b.txt").write_text(_SENTENCE * 600)
        resolved = d.resolve()
        a_path, b_path = resolved / "a.txt", resolved / "b.txt"
        a_total = len(plan_file_chunks(a_path, settings, document_name="a.txt")[0])
        b_total = len(plan_file_chunks(b_path, settings, document_name="b.txt")[0])

        real_commit = conn.commit
        calls = {"n": 0}

        def failing_commit() -> None:
            calls["n"] += 1
            if calls["n"] == 1:  # the first flush is the shared A-final + B-partial one
                raise RuntimeError("commit boom on the A+B flush")
            real_commit()

        conn.commit = failing_commit  # type: ignore[method-assign]
        ingestor = CollectionIngestor(
            ChunkStore(db),
            conn,
            settings,
            collection="col",
            resolved=resolved,
            max_workers=1,  # A fully buffers, then B — a deterministic shared flush
            progress=lambda _m: None,
        )
        with _patched_embedder(_FakeEmbedder()):
            result = _run_with_timeout(lambda: ingestor.run([a_path, b_path]))
        conn.commit = real_commit  # type: ignore[method-assign]
        assert result is not None
        _ingested, failed, _errors = cast("tuple[int, int, list[str]]", result)
        assert failed >= 1
        # G4 atomicity: neither file committed a registry row at the failed flush.
        assert conn.get_file(str(a_path)) is None
        assert conn.get_file(str(b_path)) is None

        # Clean re-sync reconciles both — contiguous, zero duplicates across both.
        with _patched_embedder(_FakeEmbedder()):
            CollectionIngestor(
                ChunkStore(db),
                conn,
                settings,
                collection="col",
                resolved=resolved,
                max_workers=1,
                progress=lambda _m: None,
            ).run([a_path, b_path])
        assert _chunk_indexes(db, "a.txt") == list(range(a_total))
        assert _chunk_indexes(db, "b.txt") == list(range(b_total))
        conn.close()

    def test_g4_crash_mid_commit_under_real_concurrency(self, tmp_path: Path):
        """G4 crash-mid-commit under max_workers=2 — the interleave is the point.

        Two large files stream concurrently; the first flush's commit raises. The
        interleaving of A's and B's windows in the failing flush is nondeterministic
        under real threads, so the assertion is the invariant: nothing commits at
        the failed flush, and a clean re-sync reconciles both with zero duplicates.
        """
        settings = _settings(tmp_path, flush_mb=1, window=8, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        (d / "a.txt").write_text(_SENTENCE * 500)
        (d / "b.txt").write_text(_SENTENCE * 500)
        resolved = d.resolve()
        a_path, b_path = resolved / "a.txt", resolved / "b.txt"

        real_commit = conn.commit
        calls = {"n": 0}

        def failing_commit() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("commit boom on the first concurrent flush")
            real_commit()

        conn.commit = failing_commit  # type: ignore[method-assign]
        ingestor = CollectionIngestor(
            ChunkStore(db),
            conn,
            settings,
            collection="col",
            resolved=resolved,
            max_workers=2,  # real concurrency: A and B interleave into the flush
            progress=lambda _m: None,
        )
        with _patched_embedder(_FakeEmbedder()):
            result = _run_with_timeout(lambda: ingestor.run([a_path, b_path]))
        conn.commit = real_commit  # type: ignore[method-assign]
        assert result is not None
        _ingested, failed, _errors = cast("tuple[int, int, list[str]]", result)
        assert failed >= 1
        assert conn.get_file(str(a_path)) is None  # neither committed (all-or-none)
        assert conn.get_file(str(b_path)) is None

        with _patched_embedder(_FakeEmbedder()):
            CollectionIngestor(
                ChunkStore(db),
                conn,
                settings,
                collection="col",
                resolved=resolved,
                max_workers=2,
                progress=lambda _m: None,
            ).run([a_path, b_path])
        for name in ("a.txt", "b.txt"):
            total = len(plan_file_chunks(d / name, settings, document_name=name)[0])
            assert _chunk_indexes(db, name) == list(range(total))
        conn.close()

    def test_raising_progress_callback_fails_cleanly_no_hang(self, tmp_path: Path):
        """Gap A: a progress callback that raises aborts + drains, never dead-locks."""
        settings = _settings(tmp_path, flush_mb=1, window=8, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        (d / "a.txt").write_text(_SENTENCE * 60)
        (d / "b.txt").write_text(_SENTENCE * 60)

        def boom_progress(message: str) -> None:
            # The user callback raises on a per-file "Ingested ..." completion,
            # which runs on the consumer thread inside the drain loop.
            if "Ingested" in message:
                raise RuntimeError("progress callback boom")

        with _patched_embedder(_FakeEmbedder()):
            result = _run_with_timeout(
                lambda: sync_collection(
                    d,
                    "col",
                    db,
                    settings,
                    conn,
                    max_workers=2,
                    progress_callback=boom_progress,
                )
            )
        assert result is not None  # did not hang — the watchdog would have failed
        assert result.failed >= 1  # type: ignore[attr-defined]
        conn.close()

    def test_bounded_queue_backpressure_caps_in_flight(self, tmp_path: Path):
        """A slow consumer makes producers block at the bounded queue's capacity."""
        settings = _settings(tmp_path, flush_mb=32, window=8, max_chars=45)
        db, conn, d = _make_collection(tmp_path, settings)
        for i in range(4):
            (d / f"f{i}.txt").write_text(_SENTENCE * 40)

        ingestor = CollectionIngestor(
            ChunkStore(db),
            conn,
            settings,
            collection="col",
            resolved=d.resolve(),
            max_workers=4,
            progress=lambda _m: None,
        )
        queue = ingestor._queue
        capacity = queue.maxsize
        assert capacity > 0  # the queue is bounded, so in-flight windows are capped

        observed: list[int] = []
        stop = threading.Event()

        def watch() -> None:
            while not stop.is_set():
                observed.append(queue.qsize())

        # Slow the single consumer so producers outpace it and fill the queue.
        real_add = ProgressiveIndexer.add_window

        def slow_add(
            self: ProgressiveIndexer, file_id: str, batch: object, vectors: object
        ) -> None:
            time.sleep(0.002)
            real_add(self, file_id, batch, vectors)  # type: ignore[arg-type]

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            with (
                _patched_embedder(_FakeEmbedder()),
                patch.object(ProgressiveIndexer, "add_window", slow_add),
            ):
                files = [d.resolve() / f"f{i}.txt" for i in range(4)]
                _run_with_timeout(lambda: ingestor.run(files))
        finally:
            stop.set()
            watcher.join()

        assert observed  # the watcher sampled the queue
        # Backpressure actually engaged: a slow consumer let producers fill the
        # bounded queue to its capacity (the load-bearing assertion — that the
        # queue never *exceeds* maxsize is a stdlib guarantee, not ours to test).
        assert max(observed) == capacity
        conn.close()


class TestSyncAll:
    def test_syncs_all_registered(self, tmp_path: Path):
        settings = _settings(tmp_path)
        conn = SyncRegistry(settings.registry_path)
        d1 = tmp_path / "a"
        d1.mkdir()
        (d1 / "one.txt").write_text(_SENTENCE * 2)
        d2 = tmp_path / "b"
        d2.mkdir()
        (d2 / "two.txt").write_text(_SENTENCE * 2)
        conn.register_directory(d1, "alpha")
        conn.register_directory(d2, "beta")
        conn.close()

        db = get_db(settings.lancedb_path)
        with _patched_embedder(_FakeEmbedder()):
            results = sync_all(db, settings, max_workers=1)

        assert results["alpha"].ingested == 1
        assert results["beta"].ingested == 1
