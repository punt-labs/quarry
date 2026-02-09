from __future__ import annotations

from pathlib import Path

from quarry.registry import open_registry


class TestOpenRegistry:
    def test_creates_tables(self, tmp_path: Path):
        db_path = tmp_path / "registry.db"
        conn = open_registry(db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "directories" in tables
        assert "files" in tables
        conn.close()

    def test_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "registry.db"
        conn1 = open_registry(db_path)
        conn1.close()
        conn2 = open_registry(db_path)
        tables = {
            row[0]
            for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "directories" in tables
        assert "files" in tables
        conn2.close()

    def test_wal_mode(self, tmp_path: Path):
        db_path = tmp_path / "registry.db"
        conn = open_registry(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode is not None
        assert mode[0] == "wal"
        conn.close()

    def test_creates_parent_directories(self, tmp_path: Path):
        db_path = tmp_path / "nested" / "dir" / "registry.db"
        conn = open_registry(db_path)
        assert db_path.exists()
        conn.close()
