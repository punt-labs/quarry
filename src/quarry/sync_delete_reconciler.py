"""Reconcile a collection's stored documents against disk to find deletions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.db import ChunkCatalog

if TYPE_CHECKING:
    from quarry.types import LanceDB


@final
class DeleteReconciler:
    """Decide which of a collection's documents a sync should delete.

    Owns the ``(db, collection)`` the two delete-reconcile operations share (the
    PY-OO-7 pattern): the registry-delta path and the LanceDB-vs-disk path.
    """

    __slots__ = ("_collection", "_db")

    _db: LanceDB
    _collection: str

    def __new__(cls, db: LanceDB, collection: str) -> Self:
        """Bind document-deletion decisions to a database and collection."""
        self = super().__new__(cls)
        self._db = db
        self._collection = collection
        return self

    def to_delete(
        self, registry_deletes: list[str], *, registry_tracked: bool
    ) -> list[str]:
        """Return the documents to delete this sync, order-stable and deduped.

        When the registry tracks the collection's files, *registry_deletes* (its
        delta) already prunes deleted files, so no per-document scan is needed.
        Only a re-adopt (registry blank, chunks present) needs the LanceDB-vs-disk
        reconcile to prune files deleted while disabled.
        """
        if registry_tracked:
            return list(dict.fromkeys(registry_deletes))
        return list(dict.fromkeys([*registry_deletes, *self._stale_documents()]))

    def _stale_documents(self) -> list[str]:
        """Return document_names for the collection whose file is DEFINITELY gone.

        The registry ``files`` rows are the delta's memory, but a keep-data disable
        deletes them while the chunks live on. On re-adopt the registry is blank,
        so the delta alone cannot prune a file deleted while disabled. This
        reconciles the ACTUAL stored documents against disk; in steady state it
        matches the registry-derived set, so it only ever prunes genuine orphans.

        Gate on the stored absolute ``document_path``, NOT ``resolved /
        document_name``: ``document_name`` is only resolved-relative for
        sync-ingested docs — the pipeline default and captures store the basename,
        so reconstructing the path would mislocate a nested doc and prune it while
        live. Prune ONLY on definite absence (``FileNotFoundError`` /
        ``NotADirectoryError``); anything unprovable is KEPT (fail-safe) and
        retried next sync — one document's error never aborts the scan.

        A non-absolute ``document_path`` is unprovable and kept: a NULL Arrow value
        stringifies to ``"None"`` (not ``""``) through ``list_documents``, and a
        relative or blank path can't be resolved against a known root here, so
        ``Path.is_absolute`` is the gate — only an absolute path can be proven
        absent.
        """
        stale: list[str] = []
        for doc in ChunkCatalog(self._db).list_documents(self._collection):
            path = str(doc["document_path"])
            if not Path(path).is_absolute():  # NULL→"None", blank, or relative → keep
                continue
            try:
                os.lstat(path)  # succeeds → present
            except (FileNotFoundError, NotADirectoryError):
                stale.append(str(doc["document_name"]))  # definite absence → prune
            except OSError:  # unreadable / IO error → cannot prove absence → keep
                continue
        return stale
