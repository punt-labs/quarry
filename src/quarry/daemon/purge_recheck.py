"""The execution-time registry re-check that gates a queued collection purge."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.sync_registry import SyncRegistry

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.db.chunk_store import ChunkStore


@final
class PurgeRecheck:
    """Purge a collection only if it is still due, re-checked under one transaction.

    A purge submitted from a stale sweep snapshot can be raced by a
    re-enable: X is marked pending and its immediate purge shed, the sweep
    snapshots X as an orphan, X is re-registered (clearing the mark, adding
    the directory row) and re-scanned, THEN this stale purge runs. Deleting
    unconditionally would wipe the re-ingested chunks — and permanently,
    since the re-scan's registry rows survive the chunk delete, so the next
    sync sees them as up to date and never re-ingests.

    So re-check at execution time (the analog of the reconcile's live-key
    skip): inside ONE registry transaction, purge ONLY if the collection is
    still marked and neither registered nor retained. Once the chunks are
    gone the mark is spent, so clearing it keeps the closed set tight and
    stops a future collection re-using the name from being swept on it.

    ``force`` bypasses the still-pending/registered checks (the self-subsume
    clean-slate purge MUST delete the widened parent's OLD narrower-root
    chunks even though it re-registered the same name), but it NEVER bypasses
    the retained guard: retained is an absolute invariant. A concurrent
    keep-data deregister can mark the collection retained (an independent
    commit, no FIFO slot) while a force purge is queued; deleting then would
    wipe the chunks keep-data was asked to preserve. So retained is checked
    standalone and always honored, force or not.
    """

    __slots__ = ("_collection", "_force", "_registry_path")

    _collection: str
    _force: bool
    _registry_path: Path

    def __new__(cls, registry_path: Path, collection: str, *, force: bool) -> Self:
        """Bind the re-check to a collection's registry and its force setting."""
        self = super().__new__(cls)
        self._collection = collection
        self._force = force
        self._registry_path = registry_path
        return self

    def delete_if_due(self, store: ChunkStore) -> int:
        """Delete the collection's chunks unless a guard refuses; return the count."""
        conn = SyncRegistry(self._registry_path)
        try:
            conn.execute("BEGIN")
            retained = self._collection in conn.markers.list_retained()
            superseded = not self._force and self._superseded_excluding_retained(conn)
            if retained or superseded:
                conn.commit()  # retained / re-registered / cleared → do NOT delete
                return 0
            deleted = store.delete_collection(self._collection)
            conn.markers.clear_pending(self._collection)
            conn.commit()
        finally:
            conn.close()
        return deleted

    def _superseded_excluding_retained(self, conn: SyncRegistry) -> bool:
        """Return whether the purge is superseded by re-registration or a spent mark.

        Reads under the caller's open transaction (one snapshot).  Retained is the
        caller's standalone absolute guard, so it is deliberately NOT re-queried
        here — one ``list_retained()`` per purge, not two.
        """
        still_pending = self._collection in conn.markers.pending()
        registered = conn.get_registration(self._collection) is not None
        return not still_pending or registered
