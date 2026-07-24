"""SQLite registry for sync: registered directories and file state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from quarry.sync_file_store import FileStore
from quarry.sync_schema import SyncSchema


@dataclass(frozen=True, slots=True)
class DirectoryRegistration:
    directory: str
    collection: str
    registered_at: str


class SyncRegistry:
    """Manages the SQLite registry for directory registrations and retained state.

    Wraps a sqlite3.Connection and exposes the directory-registration operations
    (register, deregister, list, get) plus the low-level connection interface
    (execute, commit, close) so callers can also run ad-hoc SQL. Per-file rows
    live in a composed :class:`FileStore`, reachable via :attr:`files` and backed
    by this same connection so its writes share the registry's transaction.
    """

    _conn: sqlite3.Connection
    _files: FileStore

    def __new__(cls, path: Path) -> Self:
        self = super().__new__(cls)
        path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the connection is written only from the calling
        # thread (never from ThreadPoolExecutor workers) but is passed across
        # boundaries that include threaded code paths, so disable the affinity check.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            self._ensure_schema()
        except Exception:
            self._conn.close()
            raise
        self._files = FileStore(self._conn)
        return self

    @property
    def files(self) -> FileStore:
        """Return the per-file row store sharing this registry's connection."""
        return self._files

    def _ensure_schema(self) -> None:
        """Set connection pragmas, create tables, and apply migrations."""
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait up to 5 s for a contended write lock, not instant lock error.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        schema = SyncSchema(self._conn)
        schema.initialize()
        schema.migrate()

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
    ) -> tuple[DirectoryRegistration, list[str]]:
        """Register a directory for incremental sync.

        Subsumption rules:

        - If *directory* is an ancestor of existing registrations, the children
          are deregistered (the parent subsumes them).
        - If an existing registration is an ancestor of *directory*, the
          registration is rejected — the child is already covered.

        Return the new registration and the collections it subsumed, so the
        caller can tear down each subsumed child's watch and purge its chunks
        (whose ``directories`` row this call just deleted).  The list is empty
        unless *directory* was a parent of existing registrations.

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

        subsumed = self._enforce_subsumption(resolved)

        now = datetime.now(UTC).isoformat()
        try:
            self._conn.execute(
                "INSERT INTO directories (directory, collection, registered_at) "
                "VALUES (?, ?, ?)",
                (str(resolved), collection, now),
            )
            # A re-registered collection is live again — drop any retained marker
            # so the orphan sweep stops treating its chunks as keep-data.
            self._conn.execute(
                "DELETE FROM retained_collections WHERE collection = ?",
                (collection,),
            )
        except sqlite3.IntegrityError:
            self._raise_for_integrity(resolved, collection)
        except sqlite3.Error:
            self._conn.rollback()
            raise
        self._conn.commit()
        registration = DirectoryRegistration(
            directory=str(resolved),
            collection=collection,
            registered_at=now,
        )
        return registration, subsumed

    def _enforce_subsumption(self, resolved: Path) -> list[str]:
        """Reject child-of-parent, evict children of new parent, return them."""
        existing_regs = self.list_registrations()
        for reg in existing_regs:
            reg_path = Path(reg.directory).resolve()
            if self._is_ancestor_of(reg_path, resolved):
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
            if self._is_ancestor_of(resolved, Path(reg.directory).resolve())
        ]
        for child_collection in subsumed:
            self._conn.execute(
                "DELETE FROM files WHERE collection = ?", (child_collection,)
            )
            self._conn.execute(
                "DELETE FROM directories WHERE collection = ?", (child_collection,)
            )
        return subsumed

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

    def deregister_directory(
        self, collection: str, *, keep_data: bool = False
    ) -> list[str]:
        """Remove a directory registration and its file records.

        When *keep_data* is set, record the collection as retained IN THE SAME
        transaction as the row removal, so a crash can never leave the chunks
        looking like an orphan the sweep would delete.  Return the document_names
        of files that were tracked, so the caller can clean them from LanceDB.
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
        if keep_data:
            self._conn.execute(
                "INSERT OR REPLACE INTO retained_collections "
                "(collection, retained_at) VALUES (?, ?)",
                (collection, datetime.now(UTC).isoformat()),
            )
        self._conn.commit()
        return document_names

    def list_retained(self) -> list[str]:
        """Return the collections whose chunks were deliberately kept on deregister."""
        rows = self._conn.execute(
            "SELECT collection FROM retained_collections ORDER BY collection"
        ).fetchall()
        return [r[0] for r in rows]

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

    @staticmethod
    def _is_ancestor_of(ancestor: Path, descendant: Path) -> bool:
        """Return True if *ancestor* is a strict ancestor of *descendant*.

        Both paths should be resolved (absolute, no symlinks); strict inequality
        means a path is not its own ancestor.
        """
        return ancestor != descendant and descendant.is_relative_to(ancestor)
