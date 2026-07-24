"""SQLite schema creation and idempotent migration for the sync registry."""

from __future__ import annotations

import sqlite3
from typing import Self

_DDL = """\
CREATE TABLE IF NOT EXISTS directories (
    directory     TEXT PRIMARY KEY,
    collection    TEXT NOT NULL UNIQUE,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    path             TEXT PRIMARY KEY,
    collection       TEXT NOT NULL,
    document_name    TEXT NOT NULL,
    mtime            REAL NOT NULL,
    size             INTEGER NOT NULL,
    ingested_at      TEXT NOT NULL,
    content_hash     TEXT,
    chunks_committed INTEGER NOT NULL DEFAULT 0,
    partial_hash     TEXT,
    FOREIGN KEY (collection) REFERENCES directories(collection)
);
CREATE INDEX IF NOT EXISTS idx_files_collection_path
    ON files(collection, path);
CREATE TABLE IF NOT EXISTS retained_collections (
    collection  TEXT PRIMARY KEY,
    retained_at TEXT NOT NULL
);
"""

# Columns added to the ``files`` table after v1, with their SQL type clauses.
# Each is applied only when absent so ``migrate`` is a no-op on a current DB.
_FILE_MIGRATIONS: dict[str, str] = {
    "content_hash": "TEXT",
    "chunks_committed": "INTEGER NOT NULL DEFAULT 0",
    "partial_hash": "TEXT",
}


class SyncSchema:
    """Own the registry's DDL: create tables and apply idempotent migrations."""

    __slots__ = ("_conn",)

    _conn: sqlite3.Connection

    def __new__(cls, conn: sqlite3.Connection) -> Self:
        self = super().__new__(cls)
        self._conn = conn
        return self

    def initialize(self) -> None:
        """Create the directories and files tables if they do not exist."""
        self._conn.executescript(_DDL)

    def migrate(self) -> None:
        """Add any post-v1 columns missing from an existing ``files`` table.

        Uses ``PRAGMA table_info`` to check for presence rather than catching
        ``OperationalError`` — the intent reads straight from the code, and
        running it twice is a no-op.
        """
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(files)")}
        added = False
        for column, type_clause in _FILE_MIGRATIONS.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE files ADD COLUMN {column} {type_clause}"
                )
                added = True
        if added:
            self._conn.commit()
