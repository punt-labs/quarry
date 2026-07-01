"""SQLite registry for sync: registered directories and file state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self


@dataclass(frozen=True, slots=True)
class DirectoryRegistration:
    directory: str
    collection: str
    registered_at: str


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    collection: str
    document_name: str
    mtime: float
    size: int
    ingested_at: str
    content_hash: str | None = None


class SyncRegistry:
    """Manages the SQLite registry for directory registrations and file state.

    Wraps a sqlite3.Connection and exposes both the high-level registry
    operations (register, deregister, list, get, upsert, delete) and
    the low-level connection interface (execute, commit, close) so that
    callers holding a SyncRegistry can also run ad-hoc SQL.
    """

    _conn: sqlite3.Connection

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the connection is written only from the calling
        # thread (never from ThreadPoolExecutor workers) but is passed across
        # boundaries that include threaded code paths, so disable the affinity check.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            # Wait up to 5 s for a contended write lock, not instant lock error.
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
            self._migrate_schema()
        except Exception:
            self._conn.close()
            raise
        return self

    # ------------------------------------------------------------------
    # sqlite3.Connection proxy — callers may call these directly on conn
    # ------------------------------------------------------------------

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        """Execute *sql* on the underlying connection."""
        cursor: sqlite3.Cursor = self._conn.execute(sql, parameters)
        return cursor

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        """Execute *sql_script* via the underlying connection."""
        cursor: sqlite3.Cursor = self._conn.executescript(sql_script)
        return cursor

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def rollback(self) -> None:
        """Roll back the current transaction."""
        self._conn.rollback()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Registry operations
    # ------------------------------------------------------------------

    def register_directory(
        self,
        directory: Path,
        collection: str,
    ) -> DirectoryRegistration:
        """Register a directory for incremental sync.

        Subsumption rules:

        - If *directory* is an ancestor of existing registrations, the children
          are deregistered (the parent subsumes them).
        - If an existing registration is an ancestor of *directory*, the
          registration is rejected — the child is already covered.

        Raises:
            FileNotFoundError: If *directory* does not exist.
            ValueError: If *directory* is already registered, *collection*
                name is already in use, or *directory* is a child of an
                existing registration.
        """
        resolved = directory.resolve()
        if not resolved.is_dir():
            msg = f"Directory not found: {resolved}"
            raise FileNotFoundError(msg)

        self._enforce_subsumption(resolved)

        now = datetime.now(UTC).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO directories (directory, collection, registered_at) "
                "VALUES (?, ?, ?)",
                (str(resolved), collection, now),
            )
        except sqlite3.IntegrityError:
            self._raise_for_integrity(resolved, collection)
        except sqlite3.Error:
            self._conn.rollback()
            raise
        self._conn.commit()
        return DirectoryRegistration(
            directory=str(resolved),
            collection=collection,
            registered_at=now,
        )

    def _enforce_subsumption(self, resolved: Path) -> None:
        """Reject child-of-parent, evict children of new parent."""
        existing_regs = self.list_registrations()
        for reg in existing_regs:
            reg_path = Path(reg.directory).resolve()
            if _is_ancestor_of(reg_path, resolved):
                msg = (
                    f"directory already covered by parent registration "
                    f"'{reg.collection}' ({reg.directory})"
                )
                raise ValueError(msg)
        # Inline the DELETE SQL instead of calling deregister_directory() so the
        # child removals and parent INSERT share one transaction — if the INSERT
        # fails, the children are preserved.
        subsumed = [
            reg.collection
            for reg in existing_regs
            if _is_ancestor_of(resolved, Path(reg.directory).resolve())
        ]
        for child_collection in subsumed:
            self._conn.execute(
                "DELETE FROM files WHERE collection = ?", (child_collection,)
            )
            self._conn.execute(
                "DELETE FROM directories WHERE collection = ?", (child_collection,)
            )

    def _raise_for_integrity(self, resolved: Path, collection: str) -> None:
        """Translate an INSERT IntegrityError into a precise ValueError."""
        self._conn.rollback()
        existing = self._conn.execute(
            "SELECT directory, collection FROM directories "
            "WHERE directory = ? OR collection = ?",
            (str(resolved), collection),
        ).fetchone()
        if existing and existing[0] == str(resolved):
            msg = (
                f"Directory already registered: {resolved} (collection '{existing[1]}')"
            )
        else:
            msg = f"Collection name already in use: '{collection}'"
        raise ValueError(msg) from None

    def deregister_directory(self, collection: str) -> list[str]:
        """Remove a directory registration and its file records.

        Return document_names of files that were tracked, so the
        caller can clean them from LanceDB.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT document_name FROM files WHERE collection = ? "
            "ORDER BY document_name",
            (collection,),
        ).fetchall()
        document_names = [r[0] for r in rows]
        self._conn.execute("DELETE FROM files WHERE collection = ?", (collection,))
        self._conn.execute(
            "DELETE FROM directories WHERE collection = ?",
            (collection,),
        )
        self._conn.commit()
        return document_names

    def list_registrations(self) -> list[DirectoryRegistration]:
        """Return all registered directories."""
        rows = self._conn.execute(
            "SELECT directory, collection, registered_at FROM directories "
            "ORDER BY collection"
        ).fetchall()
        return [
            DirectoryRegistration(directory=r[0], collection=r[1], registered_at=r[2])
            for r in rows
        ]

    def get_registration(self, collection: str) -> DirectoryRegistration | None:
        """Look up a single registration by collection name."""
        row = self._conn.execute(
            "SELECT directory, collection, registered_at FROM directories "
            "WHERE collection = ?",
            (collection,),
        ).fetchone()
        if row is None:
            return None
        return DirectoryRegistration(
            directory=row[0], collection=row[1], registered_at=row[2]
        )

    def get_file(self, path: str) -> FileRecord | None:
        """Look up a file record by absolute path."""
        row = self._conn.execute(
            "SELECT path, collection, document_name, mtime, size, ingested_at, "
            "content_hash FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return FileRecord(
            path=row[0],
            collection=row[1],
            document_name=row[2],
            mtime=row[3],
            size=row[4],
            ingested_at=row[5],
            content_hash=row[6],
        )

    def upsert_file(self, record: FileRecord, *, commit: bool = True) -> None:
        """Insert or replace a file record."""
        self._conn.execute(
            "INSERT OR REPLACE INTO files "
            "(path, collection, document_name, mtime, size, ingested_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.path,
                record.collection,
                record.document_name,
                record.mtime,
                record.size,
                record.ingested_at,
                record.content_hash,
            ),
        )
        if commit:
            self._conn.commit()

    def list_files(self, collection: str) -> list[FileRecord]:
        """Return all file records for a collection."""
        rows = self._conn.execute(
            "SELECT path, collection, document_name, mtime, size, ingested_at, "
            "content_hash FROM files WHERE collection = ? ORDER BY path",
            (collection,),
        ).fetchall()
        return [
            FileRecord(
                path=r[0],
                collection=r[1],
                document_name=r[2],
                mtime=r[3],
                size=r[4],
                ingested_at=r[5],
                content_hash=r[6],
            )
            for r in rows
        ]

    def delete_file(self, path: str, *, commit: bool = True) -> None:
        """Delete a single file record by path."""
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        if commit:
            self._conn.commit()

    # ------------------------------------------------------------------
    # Private schema helpers
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.executescript(
            """\
            CREATE TABLE IF NOT EXISTS directories (
                directory     TEXT PRIMARY KEY,
                collection    TEXT NOT NULL UNIQUE,
                registered_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                path          TEXT PRIMARY KEY,
                collection    TEXT NOT NULL,
                document_name TEXT NOT NULL,
                mtime         REAL NOT NULL,
                size          INTEGER NOT NULL,
                ingested_at   TEXT NOT NULL,
                content_hash  TEXT,
                FOREIGN KEY (collection) REFERENCES directories(collection)
            );
            CREATE INDEX IF NOT EXISTS idx_files_collection_path
                ON files(collection, path);
            """
        )

    def _migrate_schema(self) -> None:
        """Apply idempotent migrations for columns added after v1.

        Uses ``PRAGMA table_info`` to check for presence rather than
        catching ``OperationalError`` — the pragma is explicit and the
        intent reads straight from the code.
        """
        file_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(files)")
        }
        if "content_hash" not in file_columns:
            self._conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
            self._conn.commit()


def _is_ancestor_of(ancestor: Path, descendant: Path) -> bool:
    """Return True if *ancestor* is a strict ancestor of *descendant*.

    Both paths should be resolved (absolute, no symlinks).  Uses
    ``Path.relative_to()`` in a try/except for the containment check
    and requires strict inequality (same path is not an ancestor).
    """
    if ancestor == descendant:
        return False
    try:
        descendant.relative_to(ancestor)
    except ValueError:
        return False
    return True
