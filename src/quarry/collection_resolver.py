"""Registry-backed resolution of the collection a directory maps to."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from quarry.collection_namer import CollectionNamer

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from quarry.sync_registry import DirectoryRegistration, SyncRegistry


@final
class CollectionResolver:
    """Resolve the collection a directory maps to, over an open sync registry.

    Bundles the (registry, directory) resolution policy the session-start hook
    applies: the collection covering a cwd, the archive a directory owns (the
    re-adopt lookup), and a fresh unique name that avoids live, archived, AND
    chunk-bearing names.  These share the same registry + directory vocabulary,
    so they are methods on one resolver rather than free functions (PY-OO-7).
    The hooks-side counterpart of the client's ``Registrations`` view.
    """

    __slots__ = ("_conn",)

    _conn: SyncRegistry

    def __new__(cls, conn: SyncRegistry) -> Self:
        self = super().__new__(cls)
        self._conn = conn
        return self

    def covering_collection(self, cwd: str) -> str | None:
        """Return the collection covering *cwd* (exact or parent), else None."""
        registration = self.covering_registration(cwd)
        return registration.collection if registration is not None else None

    def covering_registration(self, cwd: str) -> DirectoryRegistration | None:
        """Return the registration covering *cwd* (exact or parent), else None.

        Walks up from *cwd* to the filesystem root; None means no registration
        covers it — the documented "no coverage" contract, not a failure. Callers
        that need the registration's own root directory (e.g. to check a marker
        file there, not just at *cwd*) use this over :meth:`covering_collection`.
        """
        reg_map = {r.directory: r for r in self._conn.list_registrations()}
        if not reg_map:
            return None
        current = Path(cwd).resolve()
        while True:
            found = reg_map.get(str(current))
            if found is not None:
                return found
            parent = current.parent
            if parent == current:
                return None
            current = parent

    def archived_collection_for(self, directory: Path) -> str | None:
        """Return the archived (keep-data) collection *directory* owns, else None.

        The same directory-identity re-adopt policy ``quarry enable`` applies over
        the wire (``Registrations.archived_collection_for``), here over the
        registry's ``retained_markers``.  A legacy blank-origin marker matches no
        resolved directory and is never re-adopted.  None is the "this directory
        owns no archive" contract.
        """
        target = str(directory.resolve())
        return next(
            (
                m.collection
                for m in self._conn.markers.retained_markers()
                if m.original_directory == target
            ),
            None,
        )

    def unique_collection_name(
        self, directory: Path, chunk_collections: AbstractSet[str]
    ) -> str:
        """Derive a name colliding with no live, archived, OR chunk-bearing name.

        Delegates to :class:`~quarry.collection_namer.CollectionNamer` over the
        union of live registrations, retained (keep-data) markers, and
        *chunk_collections* — the names that already hold chunks.  Avoiding all
        chunk-bearing names is necessary-and-sufficient for merge-safety: a
        different directory can never adopt a name that already holds another
        project's chunks (and the hash-suffix fallback is avoid-checked too),
        matching the remote client's ``Registrations`` picker exactly.

        *chunk_collections* is supplied by the caller from the daemon's catalog
        (over the wire), not read here: this module is registry-tier and never
        opens the vector engine (DES-031, the thin-client boundary).
        """
        taken = (
            {r.collection for r in self._conn.list_registrations()}
            | set(self._conn.markers.list_retained())
            | set(chunk_collections)
        )
        return CollectionNamer(directory, taken).unique()
