"""Per-file row store for the sync registry: the ``files`` table and its records."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Self, cast, final


@dataclass(frozen=True, slots=True)
class FileRecord:
    path: str
    collection: str
    document_name: str
    mtime: float
    size: int
    ingested_at: str
    content_hash: str | None = None
    # Within-file resume watermark (DES-034). ``chunks_committed`` counts the
    # contiguous chunks ``[0, chunks_committed)`` durable in LanceDB and reflected
    # here; ``partial_hash`` is the content hash the watermark was computed against
    # and, when non-NULL, marks the row incomplete so the next sync resumes within
    # the file. Both reset (0 / NULL) on completion.
    chunks_committed: int = 0
    partial_hash: str | None = None

    def __post_init__(self) -> None:
        if self.chunks_committed < 0:
            msg = f"chunks_committed must be >= 0, got {self.chunks_committed}"
            raise ValueError(msg)
        # A mid-file (partial) row must have made progress; a complete row leaves
        # partial_hash NULL. This rejects the incoherent "partial at watermark 0".
        if self.partial_hash is not None and self.chunks_committed <= 0:
            msg = "a partial resume row must have chunks_committed > 0"
            raise ValueError(msg)

    @property
    def is_partial(self) -> bool:
        """Return True when the row is a mid-file resume watermark, not complete."""
        return self.partial_hash is not None


@final
class FileStore:
    """Manage the ``files`` table's rows over the sync registry's connection.

    Shares the :class:`~quarry.sync_registry.SyncRegistry` connection so a file
    row written with ``commit=False`` participates in the caller's transaction —
    the registry's ``commit``/``rollback`` finalize or discard it, and the
    ``files`` foreign key onto ``directories`` is enforced within that one txn.
    """

    _conn: sqlite3.Connection

    def __new__(cls, conn: sqlite3.Connection) -> Self:
        self = super().__new__(cls)
        self._conn = conn
        return self

    def get_file(self, path: str) -> FileRecord | None:
        """Look up a file record by absolute path."""
        row = self._conn.execute(
            "SELECT path, collection, document_name, mtime, size, ingested_at, "
            "content_hash, chunks_committed, partial_hash FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def upsert_file(self, record: FileRecord, *, commit: bool = True) -> None:
        """Insert or replace a file record, including its resume watermark."""
        self._conn.execute(
            "INSERT OR REPLACE INTO files "
            "(path, collection, document_name, mtime, size, ingested_at, "
            "content_hash, chunks_committed, partial_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.path,
                record.collection,
                record.document_name,
                record.mtime,
                record.size,
                record.ingested_at,
                record.content_hash,
                record.chunks_committed,
                record.partial_hash,
            ),
        )
        if commit:
            self._conn.commit()

    def list_files(self, collection: str) -> list[FileRecord]:
        """Return all file records for a collection."""
        rows = self._conn.execute(
            "SELECT path, collection, document_name, mtime, size, ingested_at, "
            "content_hash, chunks_committed, partial_hash FROM files "
            "WHERE collection = ? ORDER BY path",
            (collection,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def delete_file(self, path: str, *, commit: bool = True) -> None:
        """Delete a single file record by path."""
        self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        if commit:
            self._conn.commit()

    @staticmethod
    def _row_to_record(row: tuple[object, ...]) -> FileRecord:
        """Build a FileRecord from a row in ``_SELECT_COLUMNS`` order."""
        return FileRecord(
            path=cast("str", row[0]),
            collection=cast("str", row[1]),
            document_name=cast("str", row[2]),
            mtime=cast("float", row[3]),
            size=cast("int", row[4]),
            ingested_at=cast("str", row[5]),
            content_hash=cast("str | None", row[6]),
            chunks_committed=cast("int", row[7]),
            partial_hash=cast("str | None", row[8]),
        )
