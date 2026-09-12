"""The captures collection a project's transcripts and fetched pages file into."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.collection_routing import CollectionRouting

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


@final
class CapturesCollection:
    """A project's captures collection name, derived from its base collection.

    A registered project's ``<repo>`` collection yields ``<repo>-captures``; an
    unregistered directory falls back to ``default-captures`` — the ordinary
    naming pattern run with ``default`` as the repo, never a one-off name.
    """

    _CAPTURES_SUFFIX = "-captures"
    _FALLBACK_REPO = "default"
    _VIRTUAL_NAMES = frozenset({"default-captures", "web-captures"})

    _name: str

    def __new__(cls, name: str) -> Self:
        self = super().__new__(cls)
        self._name = name
        return self

    @property
    def name(self) -> str:
        return self._name

    @classmethod
    def for_repo(cls, repo: str) -> Self:
        """Return the captures collection for a project's base collection name."""
        return cls(f"{repo}{cls._CAPTURES_SUFFIX}")

    @classmethod
    def resolve(cls, base_collection: str | None) -> Self:
        """Return the captures collection for *base_collection*, or the fallback.

        ``None`` (an unregistered working directory) yields ``default-captures``.
        """
        return cls.for_repo(base_collection or cls._FALLBACK_REPO)

    @classmethod
    def virtual_names(cls) -> frozenset[str]:
        """Return the captures collections with no source directory by design.

        The fallback (``default``) and WebFetch (``web``) buckets are never
        directory-backed, so the doctor's unlinked-captures check spares them.
        """
        return cls._VIRTUAL_NAMES

    @classmethod
    def for_cwd(cls, cwd: str, registrations: Mapping[str, str]) -> Self:
        """Resolve the captures collection for *cwd* against the sync registry.

        *registrations* maps each registered directory to its base collection.
        Walk up from *cwd* to the first registered ancestor and derive
        ``<repo>-captures``; an unregistered tree falls back to
        ``default-captures``.
        """
        return cls.resolve(CollectionRouting.covering_collection(cwd, registrations))

    @classmethod
    def for_registry_path(cls, cwd: str, registry_path: Path) -> Self:
        """Resolve the captures collection for *cwd* by reading the sync registry.

        Opens the registry at *registry_path*, snapshots its directory-to-
        collection map, and derives the captures collection.  The capture client
        cannot do this itself without importing the engine, so the daemon calls
        it server-side from the working directory the client sends.
        """
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        conn = SyncRegistry(registry_path)
        try:
            registrations = {
                r.directory: r.collection for r in conn.list_registrations()
            }
        finally:
            conn.close()
        return cls.for_cwd(cwd, registrations)
