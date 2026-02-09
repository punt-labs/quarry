from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
    _init_schema(conn)
    return conn


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
        """
    )
