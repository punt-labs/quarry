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
    content_hash: str | None = None


def open_registry(path: Path) -> sqlite3.Connection:
    """Open (or create) the registry database at *path*.

    Creates parent directories, enables WAL mode, and initializes
    the schema if the tables do not yet exist.  Runs idempotent
    migrations for columns added after the original schema shipped.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the connection is only ever written from the
    # calling thread (never from ThreadPoolExecutor workers), but it is passed
    # across function boundaries that include threaded code paths.  This flag
    # prevents spurious ProgrammingError if Python's thread-affinity check
    # triggers on the same-thread access pattern.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    _migrate_schema(conn)
    return conn


def register_directory(
    conn: sqlite3.Connection,
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

    # --- Subsumption check ---
    existing_regs = list_registrations(conn)
    for reg in existing_regs:
        reg_path = Path(reg.directory).resolve()
        if _is_ancestor_of(reg_path, resolved):
            msg = (
                f"directory already covered by parent registration "
                f"'{reg.collection}' ({reg.directory})"
            )
            raise ValueError(msg)
    # Deregister children that the new parent subsumes.  Inline the
    # DELETE SQL instead of calling deregister_directory() so the child
    # removals and parent INSERT are in a single transaction — if the
    # INSERT fails, the children are preserved.
    subsumed: list[str] = []
    for reg in existing_regs:
        reg_path = Path(reg.directory).resolve()
        if _is_ancestor_of(resolved, reg_path):
            subsumed.append(reg.collection)
    for child_collection in subsumed:
        conn.execute("DELETE FROM files WHERE collection = ?", (child_collection,))
        conn.execute(
            "DELETE FROM directories WHERE collection = ?", (child_collection,)
        )

    now = datetime.now(UTC).isoformat()
    try:
        conn.execute(
            "INSERT INTO directories (directory, collection, registered_at) "
            "VALUES (?, ?, ?)",
            (str(resolved), collection, now),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
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


def _is_ancestor_of(ancestor: Path, descendant: Path) -> bool:
    """Return True if *ancestor* is a strict ancestor of *descendant*.

    Both paths should be resolved (absolute, no symlinks).  Uses
    ``Path.is_relative_to`` (stdlib since 3.9) for the containment
    check and requires strict inequality (same path is not an ancestor).
    """
    if ancestor == descendant:
        return False
    try:
        descendant.relative_to(ancestor)
    except ValueError:
        return False
    return True


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


def upsert_file(
    conn: sqlite3.Connection, record: FileRecord, *, commit: bool = True
) -> None:
    """Insert or replace a file record."""
    conn.execute(
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
        conn.commit()


def list_files(
    conn: sqlite3.Connection,
    collection: str,
) -> list[FileRecord]:
    """Return all file records for a collection."""
    rows = conn.execute(
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
            content_hash  TEXT,
            FOREIGN KEY (collection) REFERENCES directories(collection)
        );
        CREATE INDEX IF NOT EXISTS idx_files_collection_path
            ON files(collection, path);
        """
    )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply idempotent migrations for columns added after v1.

    Uses ``PRAGMA table_info`` to check for presence rather than
    catching ``OperationalError`` — the pragma is explicit and the
    intent reads straight from the code.
    """
    file_columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
    if "content_hash" not in file_columns:
        conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
        conn.commit()
