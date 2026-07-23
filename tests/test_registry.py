from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from quarry.sync_registry import FileRecord, SyncRegistry


class TestOpenRegistry:
    def test_creates_tables(self, tmp_path: Path):
        db_path = tmp_path / "registry.db"
        conn = SyncRegistry(db_path)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "directories" in tables
        assert "files" in tables
        conn.close()

    def test_idempotent(self, tmp_path: Path):
        db_path = tmp_path / "registry.db"
        conn1 = SyncRegistry(db_path)
        conn1.close()
        conn2 = SyncRegistry(db_path)
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
        conn = SyncRegistry(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode is not None
        assert mode[0] == "wal"
        conn.close()

    def test_busy_timeout_set(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "registry.db")
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()
        assert timeout is not None
        assert timeout[0] == 5000
        conn.close()

    def test_foreign_keys_enforced(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO files (path, collection, document_name, mtime, size, "
                "ingested_at, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("/x/y.pdf", "nonexistent", "y.pdf", 1.0, 100, "2025-01-01", None),
            )
        conn.close()

    def test_creates_parent_directories(self, tmp_path: Path):
        db_path = tmp_path / "nested" / "dir" / "registry.db"
        conn = SyncRegistry(db_path)
        assert db_path.exists()
        conn.close()


class TestRegisterDirectory:
    def test_register_adds_row(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        course_dir = tmp_path / "ml-101"
        course_dir.mkdir()
        reg, subsumed = conn.register_directory(course_dir, "ml-101")
        assert reg.collection == "ml-101"
        assert reg.directory == str(course_dir.resolve())
        assert reg.registered_at != ""
        assert subsumed == []  # no existing registrations to subsume
        conn.close()

    def test_register_nonexistent_directory(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        with pytest.raises(FileNotFoundError, match="Directory not found"):
            conn.register_directory(tmp_path / "nope", "nope")
        conn.close()

    def test_register_duplicate_collection(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        d1 = tmp_path / "a"
        d1.mkdir()
        d2 = tmp_path / "b"
        d2.mkdir()
        conn.register_directory(d1, "shared")
        with pytest.raises(ValueError, match="Collection name already in use"):
            conn.register_directory(d2, "shared")
        conn.close()

    def test_register_duplicate_directory(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        d = tmp_path / "course"
        d.mkdir()
        conn.register_directory(d, "first")
        with pytest.raises(ValueError, match="Directory already registered"):
            conn.register_directory(d, "second")
        conn.close()

    # Subsumption (parent/child) and atomicity coverage lives in
    # tests/test_sync_concurrency.py::TestRegistrationSubsumption.


class TestDeregisterDirectory:
    def test_deregister_removes_rows(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        d = tmp_path / "course"
        d.mkdir()
        conn.register_directory(d, "course")
        # Insert a fake file record
        conn.execute(
            "INSERT INTO files (path, collection, document_name, mtime, size, "
            "ingested_at, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "/fake/path.pdf",
                "course",
                "path.pdf",
                100.0,
                500,
                "2025-01-01",
                None,
            ),
        )
        conn.commit()
        names = conn.deregister_directory("course")
        assert names == ["path.pdf"]
        assert conn.get_registration("course") is None
        conn.close()

    def test_deregister_returns_empty_for_unknown(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        names = conn.deregister_directory("unknown")
        assert names == []
        conn.close()


def _hold_write_lock(path: Path, barrier: threading.Barrier, hold_s: float) -> None:
    """Acquire the registry write lock, wait on the barrier, hold, then release."""
    conn = SyncRegistry(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        barrier.wait(timeout=5)
        time.sleep(hold_s)
        conn.commit()
    finally:
        conn.close()


class TestDeregisterConcurrency:
    """busy_timeout makes a contended deregister wait, not fail (quarry-xsz3)."""

    def test_deregister_waits_for_contended_write_lock(self, tmp_path: Path):
        path = tmp_path / "r.db"
        setup = SyncRegistry(path)
        directory = tmp_path / "docs"
        directory.mkdir()
        setup.register_directory(directory, "docs")
        setup.close()

        barrier = threading.Barrier(2)
        holder = threading.Thread(target=_hold_write_lock, args=(path, barrier, 0.3))
        holder.start()

        conn = SyncRegistry(path)
        try:
            # Barrier releases only after the holder owns the write lock, so the
            # deregister below is guaranteed to contend — no sleep-based timing.
            barrier.wait(timeout=5)
            names = conn.deregister_directory("docs")
        finally:
            conn.close()
            holder.join(timeout=5)

        assert names == []
        verify = SyncRegistry(path)
        try:
            assert verify.get_registration("docs") is None
        finally:
            verify.close()


class TestListAndGetRegistrations:
    def test_list_registrations(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        d1 = tmp_path / "a"
        d1.mkdir()
        d2 = tmp_path / "b"
        d2.mkdir()
        conn.register_directory(d1, "alpha")
        conn.register_directory(d2, "beta")
        regs = conn.list_registrations()
        assert len(regs) == 2
        assert regs[0].collection == "alpha"
        assert regs[1].collection == "beta"
        conn.close()

    def test_get_registration_found(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        d = tmp_path / "x"
        d.mkdir()
        conn.register_directory(d, "x")
        reg = conn.get_registration("x")
        assert reg is not None
        assert reg.collection == "x"
        conn.close()

    def test_get_registration_not_found(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        assert conn.get_registration("missing") is None
        conn.close()


class TestFileRecordOperations:
    def _register(self, conn: SyncRegistry, tmp_path: Path, collection: str) -> None:
        """Register a directory for *collection* so FK constraints pass."""
        d = tmp_path / f"dir-{collection}"
        d.mkdir(exist_ok=True)
        conn.register_directory(d, collection)

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
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path, "c")
        rec = self._make_record()
        conn.upsert_file(rec)
        got = conn.get_file(rec.path)
        assert got is not None
        assert got.mtime == 1000.0
        assert got.size == 2048
        conn.close()

    def test_upsert_updates_existing(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path, "c")
        rec = self._make_record()
        conn.upsert_file(rec)
        updated = FileRecord(
            path=rec.path,
            collection=rec.collection,
            document_name=rec.document_name,
            mtime=2000.0,
            size=4096,
            ingested_at="2025-06-02T00:00:00",
        )
        conn.upsert_file(updated)
        got = conn.get_file(rec.path)
        assert got is not None
        assert got.mtime == 2000.0
        assert got.size == 4096
        conn.close()

    def test_get_file_not_found(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        assert conn.get_file("/nonexistent") is None
        conn.close()

    def test_list_files_filters_by_collection(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path, "alpha")
        self._register(conn, tmp_path, "beta")
        conn.upsert_file(self._make_record("/a/1.pdf", "alpha"))
        conn.upsert_file(self._make_record("/a/2.pdf", "alpha"))
        conn.upsert_file(self._make_record("/b/3.pdf", "beta"))
        alpha_files = conn.list_files("alpha")
        assert len(alpha_files) == 2
        beta_files = conn.list_files("beta")
        assert len(beta_files) == 1
        conn.close()

    def test_delete_file_removes_record(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path, "c")
        rec = self._make_record()
        conn.upsert_file(rec)
        conn.delete_file(rec.path)
        assert conn.get_file(rec.path) is None
        conn.close()


class TestContentHashColumn:
    """Coverage for the ``content_hash`` column added in quarry-272m."""

    def test_files_table_schema_has_content_hash_column(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        assert "content_hash" in columns
        conn.close()

    def test_migration_adds_content_hash_column_to_pre_existing_db(
        self, tmp_path: Path
    ):
        """Open a v1 registry (no content_hash column) and verify migration.

        Builds the original 6-column schema by hand, inserts a real row,
        then constructs ``SyncRegistry`` which must add the column without
        dropping the existing row.
        """
        db_path = tmp_path / "legacy.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(
            """
            CREATE TABLE directories (
                directory     TEXT PRIMARY KEY,
                collection    TEXT NOT NULL UNIQUE,
                registered_at TEXT NOT NULL
            );
            CREATE TABLE files (
                path          TEXT PRIMARY KEY,
                collection    TEXT NOT NULL,
                document_name TEXT NOT NULL,
                mtime         REAL NOT NULL,
                size          INTEGER NOT NULL,
                ingested_at   TEXT NOT NULL,
                FOREIGN KEY (collection) REFERENCES directories(collection)
            );
            INSERT INTO directories VALUES ('/legacy', 'legacy', '2025-01-01');
            INSERT INTO files VALUES (
                '/legacy/a.pdf', 'legacy', 'a.pdf', 1.0, 100, '2025-01-01'
            );
            """
        )
        raw.commit()
        raw.close()

        conn = SyncRegistry(db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        assert "content_hash" in columns

        rec = conn.get_file("/legacy/a.pdf")
        assert rec is not None
        assert rec.path == "/legacy/a.pdf"
        assert rec.content_hash is None
        conn.close()

    def _register(self, conn: SyncRegistry, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir(exist_ok=True)
        conn.register_directory(d, "c")

    def test_upsert_file_round_trips_content_hash(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path)
        rec = FileRecord(
            path="/p/a.pdf",
            collection="c",
            document_name="a.pdf",
            mtime=1.0,
            size=10,
            ingested_at="2025-01-01",
            content_hash="deadbeef",
        )
        conn.upsert_file(rec)

        got = conn.get_file("/p/a.pdf")
        assert got is not None
        assert got.content_hash == "deadbeef"

        listed = conn.list_files("c")
        assert len(listed) == 1
        assert listed[0].content_hash == "deadbeef"
        conn.close()

    def test_upsert_file_allows_none_content_hash(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path)
        rec = FileRecord(
            path="/p/a.pdf",
            collection="c",
            document_name="a.pdf",
            mtime=1.0,
            size=10,
            ingested_at="2025-01-01",
        )
        conn.upsert_file(rec)

        got = conn.get_file("/p/a.pdf")
        assert got is not None
        assert got.content_hash is None
        conn.close()


class TestResumeWatermark:
    """DES-034: chunks_committed / partial_hash columns and atomic watermarks."""

    def _register(self, conn: SyncRegistry, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir(exist_ok=True)
        conn.register_directory(d, "c")

    def _record(self, **overrides: object) -> FileRecord:
        base: dict[str, object] = {
            "path": "/p/a.txt",
            "collection": "c",
            "document_name": "a.txt",
            "mtime": 1.0,
            "size": 10,
            "ingested_at": "2025-01-01",
        }
        base.update(overrides)
        return FileRecord(**base)  # type: ignore[arg-type]

    def test_defaults_are_complete(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path)
        conn.upsert_file(self._record())
        got = conn.get_file("/p/a.txt")
        assert got is not None
        assert got.chunks_committed == 0
        assert got.partial_hash is None
        assert got.is_partial is False
        conn.close()

    def test_partial_with_zero_watermark_is_rejected(self):
        """A partial row (partial_hash set) must have chunks_committed > 0."""
        with pytest.raises(ValueError, match="chunks_committed > 0"):
            self._record(partial_hash="h", chunks_committed=0)

    def test_negative_watermark_is_rejected(self):
        with pytest.raises(ValueError, match="chunks_committed must be >= 0"):
            self._record(chunks_committed=-1)

    def test_complete_row_with_positive_count_is_valid(self):
        # completion sets chunks_committed = total and partial_hash = None
        rec = self._record(chunks_committed=12, partial_hash=None)
        assert rec.is_partial is False
        assert rec.chunks_committed == 12

    def test_upsert_sets_partial_watermark(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path)
        conn.upsert_file(
            self._record(content_hash="cafe", chunks_committed=7, partial_hash="cafe")
        )
        got = conn.get_file("/p/a.txt")
        assert got is not None
        assert got.chunks_committed == 7
        assert got.partial_hash == "cafe"
        assert got.is_partial is True
        conn.close()

    def test_completion_clears_partial(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        self._register(conn, tmp_path)
        conn.upsert_file(
            self._record(content_hash="cafe", chunks_committed=7, partial_hash="cafe")
        )
        conn.upsert_file(self._record(content_hash="cafe", chunks_committed=12))
        got = conn.get_file("/p/a.txt")
        assert got is not None
        assert got.chunks_committed == 12
        assert got.partial_hash is None
        assert got.is_partial is False
        conn.close()

    def test_atomic_multi_file_commit(self, tmp_path: Path):
        """Two files' watermarks commit as one transaction (G4)."""
        db_path = tmp_path / "r.db"
        conn = SyncRegistry(db_path)
        self._register(conn, tmp_path)
        conn.upsert_file(
            self._record(path="/p/a.txt", content_hash="h", chunks_committed=5),
            commit=False,
        )
        conn.upsert_file(
            self._record(
                path="/p/b.txt",
                document_name="b.txt",
                content_hash="h",
                chunks_committed=3,
                partial_hash="h",
            ),
            commit=True,
        )

        verify = SyncRegistry(db_path)
        a = verify.get_file("/p/a.txt")
        b = verify.get_file("/p/b.txt")
        assert a is not None and a.chunks_committed == 5 and a.partial_hash is None
        assert b is not None and b.chunks_committed == 3 and b.partial_hash == "h"
        verify.close()
        conn.close()

    def test_crash_before_commit_persists_nothing(self, tmp_path: Path):
        """An uncommitted watermark is not visible from a fresh connection."""
        db_path = tmp_path / "r.db"
        conn = SyncRegistry(db_path)
        self._register(conn, tmp_path)
        conn.upsert_file(
            self._record(content_hash="h", chunks_committed=5, partial_hash="h"),
            commit=False,
        )
        # Simulate a crash: close without commit (rollback of the open txn).
        conn.rollback()
        conn.close()

        verify = SyncRegistry(db_path)
        assert verify.get_file("/p/a.txt") is None
        verify.close()

    def test_migrate_schema_is_idempotent(self, tmp_path: Path):
        """Running the schema migration twice leaves columns and rows intact."""
        from quarry.sync_schema import SyncSchema

        db_path = tmp_path / "r.db"
        conn = SyncRegistry(db_path)
        self._register(conn, tmp_path)
        conn.upsert_file(
            self._record(content_hash="h", chunks_committed=4, partial_hash="h")
        )

        schema = SyncSchema(conn._conn)
        schema.migrate()
        schema.migrate()  # second run must be a no-op

        got = conn.get_file("/p/a.txt")
        assert got is not None
        assert got.chunks_committed == 4
        assert got.partial_hash == "h"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
        assert {"content_hash", "chunks_committed", "partial_hash"} <= columns
        conn.close()

    def test_legacy_db_gains_columns(self, tmp_path: Path):
        """A pre-DES-034 files table gains the watermark columns on open."""
        db_path = tmp_path / "legacy.db"
        raw = sqlite3.connect(str(db_path))
        raw.executescript(
            """
            CREATE TABLE directories (
                directory TEXT PRIMARY KEY, collection TEXT NOT NULL UNIQUE,
                registered_at TEXT NOT NULL
            );
            CREATE TABLE files (
                path TEXT PRIMARY KEY, collection TEXT NOT NULL,
                document_name TEXT NOT NULL, mtime REAL NOT NULL,
                size INTEGER NOT NULL, ingested_at TEXT NOT NULL
            );
            """
        )
        raw.execute("INSERT INTO directories VALUES ('/p', 'c', '2025-01-01')")
        raw.execute(
            "INSERT INTO files VALUES ('/p/a.txt','c','a.txt',1.0,10,'2025-01-01')"
        )
        raw.commit()
        raw.close()

        conn = SyncRegistry(db_path)  # migration runs in __new__
        got = conn.get_file("/p/a.txt")
        assert got is not None
        assert got.content_hash is None
        assert got.chunks_committed == 0
        assert got.partial_hash is None
        conn.close()


class TestRetainedCollections:
    """The durable keep-data marker the orphan sweep consults."""

    def test_keep_data_deregister_marks_retained(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        directory = tmp_path / "docs"
        directory.mkdir()
        try:
            conn.register_directory(directory, "docs")
            assert conn.list_retained() == []
            conn.deregister_directory("docs", keep_data=True)
            assert conn.list_retained() == ["docs"]  # kept → retained, spared
        finally:
            conn.close()

    def test_plain_deregister_does_not_retain(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        directory = tmp_path / "docs"
        directory.mkdir()
        try:
            conn.register_directory(directory, "docs")
            conn.deregister_directory("docs")  # keep_data defaults False
            assert conn.list_retained() == []  # no marker → sweep may purge
        finally:
            conn.close()

    def test_reregister_clears_retained(self, tmp_path: Path):
        conn = SyncRegistry(tmp_path / "r.db")
        directory = tmp_path / "docs"
        directory.mkdir()
        try:
            conn.register_directory(directory, "docs")
            conn.deregister_directory("docs", keep_data=True)
            assert conn.list_retained() == ["docs"]
            conn.register_directory(directory, "docs")  # live again
            assert conn.list_retained() == []  # marker cleared on re-register
        finally:
            conn.close()
