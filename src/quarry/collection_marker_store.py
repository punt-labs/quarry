"""Retained + pending-purge collection markers over the sync registry's connection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self, final


@dataclass(frozen=True, slots=True)
class RetainedMarker:
    """An archived (keep-data) collection and the directory it was kept from."""

    collection: str
    original_directory: str


@final
class CollectionMarkerStore:
    """Own the ``retained_collections`` and ``pending_purge_collections`` markers.

    Shares the :class:`~quarry.sync_registry.SyncRegistry` connection (the
    ``FileStore`` pattern), so every ``mark_*``/``clear_*`` executes on the same
    connection as the ``directories`` row change and commits with it — the marker
    write and the registry-row change are one transaction, atomic together. The
    caller owns the commit; these methods never commit.

    Two marker kinds: a RETAINED marker (keep-data disable, tagged with its origin
    directory so only that directory re-adopts) and a PENDING-PURGE marker (a
    non-keep deregister or a subsume eviction) — the closed set the orphan sweep
    is allowed to purge.  A collection never marked is structurally unreachable by
    the sweep, so captures, agent memories, and remember targets are safe.
    """

    _conn: sqlite3.Connection

    def __new__(cls, conn: sqlite3.Connection) -> Self:
        """Share the registry connection for retained and pending-purge markers."""
        self = super().__new__(cls)
        self._conn = conn
        return self

    def mark_retained(self, collection: str, original_directory: str | None) -> None:
        """Record *collection* as keep-data retained, tagged with its origin dir."""
        self._conn.execute(
            "INSERT OR REPLACE INTO retained_collections "
            "(collection, retained_at, original_directory) VALUES (?, ?, ?)",
            (collection, datetime.now(UTC).isoformat(), original_directory),
        )

    def clear_retained(self, collection: str) -> None:
        """Drop *collection*'s retained marker (re-adopted or superseded)."""
        self._conn.execute(
            "DELETE FROM retained_collections WHERE collection = ?", (collection,)
        )

    def list_retained(self) -> list[str]:
        """Return the collections whose chunks were deliberately kept."""
        rows = self._conn.execute(
            "SELECT collection FROM retained_collections ORDER BY collection"
        ).fetchall()
        return [r[0] for r in rows]

    def retained_markers(self) -> list[RetainedMarker]:
        """Return each retained collection with the directory it was kept from.

        A NULL ``original_directory`` (legacy marker) becomes the empty string,
        which matches no resolved directory — avoided by the name-picker, never
        re-adopted.
        """
        rows = self._conn.execute(
            "SELECT collection, original_directory FROM retained_collections "
            "ORDER BY collection"
        ).fetchall()
        return [
            RetainedMarker(collection=r[0], original_directory=r[1] or "") for r in rows
        ]

    def retained_marker(self, collection: str) -> RetainedMarker | None:
        """Return *collection*'s retained marker, or None when it has none."""
        row = self._conn.execute(
            "SELECT original_directory FROM retained_collections WHERE collection = ?",
            (collection,),
        ).fetchone()
        if row is None:
            return None
        return RetainedMarker(collection=collection, original_directory=row[0] or "")

    def mark_pending(self, collection: str) -> None:
        """Flag *collection* for purge (non-keep deregister or subsume eviction)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO pending_purge_collections "
            "(collection, marked_at) VALUES (?, ?)",
            (collection, datetime.now(UTC).isoformat()),
        )

    def clear_pending(self, collection: str) -> None:
        """Drop *collection*'s purge mark (re-registered, kept, or purged)."""
        self._conn.execute(
            "DELETE FROM pending_purge_collections WHERE collection = ?",
            (collection,),
        )

    def pending(self) -> set[str]:
        """Return the collections the registry explicitly flagged for purge."""
        rows = self._conn.execute(
            "SELECT collection FROM pending_purge_collections"
        ).fetchall()
        return {r[0] for r in rows}
