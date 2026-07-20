"""The captures collection a project's transcripts and fetched pages file into."""

from __future__ import annotations

from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping


@final
class CapturesCollection:
    """A project's captures collection name, derived from its base collection.

    A registered project's ``<repo>`` collection yields ``<repo>-captures``; an
    unregistered directory falls back to ``default-captures`` — the ordinary
    naming pattern run with ``default`` as the repo, never a one-off name.
    """

    _CAPTURES_SUFFIX = "-captures"
    _FALLBACK_REPO = "default"

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
    def fallback(cls) -> Self:
        """Return the collection an unregistered directory's captures fall into.

        This is the live ``default-captures`` fallback (not a base-less one-off),
        so the doctor's orphan check must spare it — its base ``default`` is never
        registered by design.
        """
        return cls.resolve(None)

    @classmethod
    def for_cwd(cls, cwd: str, registrations: Mapping[str, str]) -> Self:
        """Resolve the captures collection for *cwd* against the sync registry.

        *registrations* maps each registered directory to its base collection.
        Walk up from *cwd* to the first registered ancestor and derive
        ``<repo>-captures``; an unregistered tree falls back to
        ``default-captures``.
        """
        return cls.resolve(cls._covering_collection(cwd, registrations))

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

    @staticmethod
    def _covering_collection(cwd: str, registrations: Mapping[str, str]) -> str | None:
        """Return the base collection of the registered ancestor of *cwd*."""
        # A blank or RELATIVE cwd is "unregistered", not the daemon's own dir:
        # both resolve against the daemon PROCESS's cwd, which — if quarryd was
        # started inside a repo checkout — would misfile the capture into that
        # project.  cwd is untrusted client input; only an absolute path names a
        # real client directory.
        if not registrations or not Path(cwd).is_absolute():
            return None
        try:
            current = Path(cwd).resolve()
        except (OSError, ValueError):
            # An embedded NUL or OS-invalid path falls back to default-captures.
            return None
        # Iterate ancestors lazily; never materialize the full parent list —
        # untrusted deep cwd (``/a/a/.../a``) would retain O(depth²) prefixes.
        for path in chain((current,), current.parents):
            if (collection := registrations.get(str(path))) is not None:
                return collection
        return None
