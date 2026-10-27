"""SQLite schema creation and idempotent migration for the query log."""

from __future__ import annotations

import sqlite3
from typing import Self

_DDL = """\
CREATE TABLE IF NOT EXISTS query_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    surface        TEXT NOT NULL DEFAULT '',
    agent_handle   TEXT NOT NULL DEFAULT '',
    collection     TEXT NOT NULL DEFAULT '',
    filters_json   TEXT NOT NULL DEFAULT '{}',
    limit_n        INTEGER NOT NULL,
    latency_ms     REAL NOT NULL,
    result_count   INTEGER NOT NULL,
    query_scrubbed TEXT NOT NULL,
    query_len      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_events_ts ON query_events(ts);
CREATE INDEX IF NOT EXISTS idx_query_events_agent ON query_events(agent_handle);
CREATE TABLE IF NOT EXISTS query_hits (
    query_event_id   INTEGER NOT NULL,
    rank             INTEGER NOT NULL,
    document_name    TEXT NOT NULL,
    collection       TEXT NOT NULL,
    chunk_index      INTEGER NOT NULL,
    score            REAL NOT NULL,
    hit_agent_handle TEXT NOT NULL DEFAULT '',
    memory_type      TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (query_event_id) REFERENCES query_events(id)
);
CREATE INDEX IF NOT EXISTS idx_query_hits_event ON query_hits(query_event_id);
CREATE INDEX IF NOT EXISTS idx_query_hits_collection ON query_hits(collection);
"""

# Post-v1 columns per table, with their SQL type clauses -- the query-log twin of
# ``sync_schema._MIGRATIONS``.  Empty today (this is the v1 schema); the
# machinery exists so a future column lands as a migration, not a breaking
# rebuild, matching the registry's own idempotent-migration contract.
_MIGRATIONS: dict[str, dict[str, str]] = {}


class QueryLogSchema:
    """Own the query log's DDL: create tables and apply idempotent migrations."""

    __slots__ = ("_conn",)

    _conn: sqlite3.Connection

    def __new__(cls, conn: sqlite3.Connection) -> Self:
        self = super().__new__(cls)
        self._conn = conn
        return self

    def initialize(self) -> None:
        """Create the query_events and query_hits tables if they do not exist."""
        self._conn.executescript(_DDL)

    def migrate(self) -> None:
        """Add any post-v1 columns missing from the query log's tables.

        Uses ``PRAGMA table_info`` to check for presence rather than catching
        ``OperationalError`` — the intent reads straight from the code, and
        running it twice is a no-op.
        """
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
