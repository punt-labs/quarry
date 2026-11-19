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
    collection         TEXT PRIMARY KEY,
    retained_at        TEXT NOT NULL,
    original_directory TEXT
);
CREATE TABLE IF NOT EXISTS pending_purge_collections (
    collection TEXT PRIMARY KEY,
    marked_at  TEXT NOT NULL
);
"""

# Post-v1 columns per table, with their SQL type clauses. Each is applied only
# when absent so ``migrate`` is a no-op on a current DB. ``original_directory``
# on retained_collections is nullable: legacy markers written before this column
# carry NULL, which the registry treats as "origin unknown" (see SyncRegistry).
_MIGRATIONS: dict[str, dict[str, str]] = {
    "files": {
        "content_hash": "TEXT",
        "chunks_committed": "INTEGER NOT NULL DEFAULT 0",
        "partial_hash": "TEXT",
    },
    "retained_collections": {
        "original_directory": "TEXT",
    },
}


class SyncSchema:
    """Own the registry's DDL: create tables and apply idempotent migrations."""

    __slots__ = ("_conn",)

    _conn: sqlite3.Connection

    def __new__(cls, conn: sqlite3.Connection) -> Self:
        """Bind registry DDL and migrations to an open SQLite connection."""
        self = super().__new__(cls)
        self._conn = conn
        return self

    def initialize(self) -> None:
        """Create the directories and files tables if they do not exist."""
        self._conn.executescript(_DDL)

    def migrate(self) -> None:
        """Add any post-v1 columns missing from the registry's tables.

        Uses ``PRAGMA table_info`` to check for presence rather than catching
        ``OperationalError`` — the intent reads straight from the code, and
        running it twice is a no-op.
        """
        # Force the list so every table is migrated before the any() check —
        # a short-circuiting generator would skip tables after the first change.
        changed = [
            self._add_missing_columns(table, columns)
            for table, columns in _MIGRATIONS.items()
        ]
        if any(changed):
            self._conn.commit()

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> bool:
        """Add each absent column to *table*; return whether any were added."""
        existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        added = False
        for column, type_clause in columns.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {type_clause}"
                )
                added = True
        return added
