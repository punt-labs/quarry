"""SQLite registry for sync: registered directories and file state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class DirectoryRegistration:
    directory: str
    collection: str
    registered_at: str


@dataclass(frozen=True)
class FileRecord:
    path: str
    collection: str
    document_name: str
    mtime: float
    size: int
    ingested_at: str


def open_registry(path: Path) -> sqlite3.Connection:
    """Open (or create) the registry database at *path*.

    Creates parent directories, enables WAL mode, and initializes
    the schema if the tables do not yet exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def register_directory(
    conn: sqlite3.Connection,
    directory: Path,
    collection: str,
) -> DirectoryRegistration:
    """Register a directory for incremental sync.

    Raises:
        FileNotFoundError: If *directory* does not exist.
        ValueError: If *directory* is already registered or *collection*
            name is already in use.
    """
    resolved = directory.resolve()
    if not resolved.is_dir():
        msg = f"Directory not found: {resolved}"
        raise FileNotFoundError(msg)
    now = datetime.now(UTC).isoformat()
    try:
        conn.execute(
            "INSERT INTO directories (directory, collection, registered_at) "
            "VALUES (?, ?, ?)",
            (str(resolved), collection, now),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
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
    conn.commit()
    return DirectoryRegistration(
        directory=str(resolved),
        collection=collection,
        registered_at=now,
    )


def deregister_directory(
    conn: sqlite3.Connection,
    collection: str,
) -> list[str]:
    """Remove a directory registration and its file records.

    Returns document_names of files that were tracked, so the
    caller can clean them from LanceDB.
    """
    rows = conn.execute(
        "SELECT DISTINCT document_name FROM files WHERE collection = ? "
        "ORDER BY document_name",
        (collection,),
    ).fetchall()
    document_names = [r[0] for r in rows]
    conn.execute("DELETE FROM files WHERE collection = ?", (collection,))
    conn.execute(
        "DELETE FROM directories WHERE collection = ?",
        (collection,),
    )
    conn.commit()
    return document_names


def list_registrations(
    conn: sqlite3.Connection,
) -> list[DirectoryRegistration]:
    """Return all registered directories."""
    rows = conn.execute(
        "SELECT directory, collection, registered_at FROM directories "
        "ORDER BY collection"
    ).fetchall()
    return [
        DirectoryRegistration(directory=r[0], collection=r[1], registered_at=r[2])
        for r in rows
    ]


def get_registration(
    conn: sqlite3.Connection,
    collection: str,
) -> DirectoryRegistration | None:
    """Look up a single registration by collection name."""
    row = conn.execute(
        "SELECT directory, collection, registered_at FROM directories "
        "WHERE collection = ?",
        (collection,),
    ).fetchone()
    if row is None:
        return None
    return DirectoryRegistration(
        directory=row[0], collection=row[1], registered_at=row[2]
    )


def get_file(conn: sqlite3.Connection, path: str) -> FileRecord | None:
    """Look up a file record by absolute path."""
    row = conn.execute(
        "SELECT path, collection, document_name, mtime, size, ingested_at "
        "FROM files WHERE path = ?",
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
    )


def upsert_file(
    conn: sqlite3.Connection, record: FileRecord, *, commit: bool = True
) -> None:
    """Insert or replace a file record."""
    conn.execute(
        "INSERT OR REPLACE INTO files "
        "(path, collection, document_name, mtime, size, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            record.path,
            record.collection,
            record.document_name,
            record.mtime,
            record.size,
            record.ingested_at,
        ),
    )
    if commit:
        conn.commit()


def list_files(
    conn: sqlite3.Connection,
    collection: str,
) -> list[FileRecord]:
    """Return all file records for a collection."""
    rows = conn.execute(
        "SELECT path, collection, document_name, mtime, size, ingested_at "
        "FROM files WHERE collection = ? ORDER BY path",
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
        )
        for r in rows
    ]


def delete_file(conn: sqlite3.Connection, path: str, *, commit: bool = True) -> None:
    """Delete a single file record by path."""
    conn.execute("DELETE FROM files WHERE path = ?", (path,))
    if commit:
        conn.commit()


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
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
            FOREIGN KEY (collection) REFERENCES directories(collection)
        );
        CREATE INDEX IF NOT EXISTS idx_files_collection_path
            ON files(collection, path);
        """
    )
