from __future__ import annotations

import time
from pathlib import Path

from quarry.registry import FileRecord, open_registry, upsert_file
from quarry.sync import compute_sync_plan, discover_files


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


class TestComputeSyncPlan:
    EXTS = frozenset({".pdf", ".txt"})

    def test_new_file_detected(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()
        (d / "new.pdf").write_bytes(b"data")
        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "new.pdf"
        assert plan.to_delete == []
        assert plan.unchanged == 0
        conn.close()

    def test_unchanged_file_skipped(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()
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
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()
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
        # Modify the file — ensure mtime changes
        time.sleep(0.05)
        f.write_bytes(b"new content that is longer")
        plan = compute_sync_plan(d, "col", conn, self.EXTS)
        assert len(plan.to_ingest) == 1
        assert plan.to_ingest[0].name == "changed.pdf"
        conn.close()

    def test_deleted_file_detected(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()
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
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "docs"
        d.mkdir()

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
