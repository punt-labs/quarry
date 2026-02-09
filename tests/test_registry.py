from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quarry.registry import (
    FileRecord,
    delete_file,
    deregister_directory,
    get_file,
    get_registration,
    list_files,
    list_registrations,
    open_registry,
    register_directory,
    upsert_file,
)


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

    def test_foreign_keys_enforced(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
                ("/x/y.pdf", "nonexistent", "y.pdf", 1.0, 100, "2025-01-01"),
            )
        conn.close()

    def test_creates_parent_directories(self, tmp_path: Path):
        db_path = tmp_path / "nested" / "dir" / "registry.db"
        conn = open_registry(db_path)
        assert db_path.exists()
        conn.close()


class TestRegisterDirectory:
    def test_register_adds_row(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        course_dir = tmp_path / "ml-101"
        course_dir.mkdir()
        reg = register_directory(conn, course_dir, "ml-101")
        assert reg.collection == "ml-101"
        assert reg.directory == str(course_dir.resolve())
        assert reg.registered_at != ""
        conn.close()

    def test_register_nonexistent_directory(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        with pytest.raises(FileNotFoundError, match="Directory not found"):
            register_directory(conn, tmp_path / "nope", "nope")
        conn.close()

    def test_register_duplicate_collection(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d1 = tmp_path / "a"
        d1.mkdir()
        d2 = tmp_path / "b"
        d2.mkdir()
        register_directory(conn, d1, "shared")
        with pytest.raises(ValueError, match="Collection name already in use"):
            register_directory(conn, d2, "shared")
        conn.close()

    def test_register_duplicate_directory(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "course"
        d.mkdir()
        register_directory(conn, d, "first")
        with pytest.raises(ValueError, match="Directory already registered"):
            register_directory(conn, d, "second")
        conn.close()


class TestDeregisterDirectory:
    def test_deregister_removes_rows(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "course"
        d.mkdir()
        register_directory(conn, d, "course")
        # Insert a fake file record
        conn.execute(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?)",
            ("/fake/path.pdf", "course", "path.pdf", 100.0, 500, "2025-01-01"),
        )
        conn.commit()
        names = deregister_directory(conn, "course")
        assert names == ["path.pdf"]
        assert get_registration(conn, "course") is None
        conn.close()

    def test_deregister_returns_empty_for_unknown(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        names = deregister_directory(conn, "unknown")
        assert names == []
        conn.close()


class TestListAndGetRegistrations:
    def test_list_registrations(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d1 = tmp_path / "a"
        d1.mkdir()
        d2 = tmp_path / "b"
        d2.mkdir()
        register_directory(conn, d1, "alpha")
        register_directory(conn, d2, "beta")
        regs = list_registrations(conn)
        assert len(regs) == 2
        assert regs[0].collection == "alpha"
        assert regs[1].collection == "beta"
        conn.close()

    def test_get_registration_found(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        d = tmp_path / "x"
        d.mkdir()
        register_directory(conn, d, "x")
        reg = get_registration(conn, "x")
        assert reg is not None
        assert reg.collection == "x"
        conn.close()

    def test_get_registration_not_found(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        assert get_registration(conn, "missing") is None
        conn.close()


class TestFileRecordOperations:
    def _register(
        self, conn: sqlite3.Connection, tmp_path: Path, collection: str
    ) -> None:
        """Register a directory for *collection* so FK constraints pass."""
        d = tmp_path / f"dir-{collection}"
        d.mkdir(exist_ok=True)
        register_directory(conn, d, collection)

    def _make_record(
        self,
        path: str = "/a/b.pdf",
        collection: str = "c",
    ) -> FileRecord:
        return FileRecord(
            path=path,
            collection=collection,
            document_name="b.pdf",
            mtime=1000.0,
            size=2048,
            ingested_at="2025-06-01T00:00:00",
        )

    def test_upsert_inserts_new(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        self._register(conn, tmp_path, "c")
        rec = self._make_record()
        upsert_file(conn, rec)
        got = get_file(conn, rec.path)
        assert got is not None
        assert got.mtime == 1000.0
        assert got.size == 2048
        conn.close()

    def test_upsert_updates_existing(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        self._register(conn, tmp_path, "c")
        rec = self._make_record()
        upsert_file(conn, rec)
        updated = FileRecord(
            path=rec.path,
            collection=rec.collection,
            document_name=rec.document_name,
            mtime=2000.0,
            size=4096,
            ingested_at="2025-06-02T00:00:00",
        )
        upsert_file(conn, updated)
        got = get_file(conn, rec.path)
        assert got is not None
        assert got.mtime == 2000.0
        assert got.size == 4096
        conn.close()

    def test_get_file_not_found(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        assert get_file(conn, "/nonexistent") is None
        conn.close()

    def test_list_files_filters_by_collection(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        self._register(conn, tmp_path, "alpha")
        self._register(conn, tmp_path, "beta")
        upsert_file(conn, self._make_record("/a/1.pdf", "alpha"))
        upsert_file(conn, self._make_record("/a/2.pdf", "alpha"))
        upsert_file(conn, self._make_record("/b/3.pdf", "beta"))
        alpha_files = list_files(conn, "alpha")
        assert len(alpha_files) == 2
        beta_files = list_files(conn, "beta")
        assert len(beta_files) == 1
        conn.close()

    def test_delete_file_removes_record(self, tmp_path: Path):
        conn = open_registry(tmp_path / "r.db")
        self._register(conn, tmp_path, "c")
        rec = self._make_record()
        upsert_file(conn, rec)
        delete_file(conn, rec.path)
        assert get_file(conn, rec.path) is None
        conn.close()
