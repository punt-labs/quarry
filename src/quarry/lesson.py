"""Naming and collection routing for ``quarry learn``'s distilled lessons."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Self, final
from uuid import uuid4

from quarry.collection_routing import CollectionRouting

if TYPE_CHECKING:
    from collections.abc import Mapping


@final
class LessonComposer:
    """Compose a collision-proof document name for a distilled lesson."""

    __slots__ = ()

    _FALLBACK_SLUG = "note"
    _MAX_SLUG_LEN = 40

    @classmethod
    def document_name(cls, name: str, topic: str) -> str:
        """Return ``lesson-<slug>-<8 hex>``.

        The slug is human-relatable (from *name*, else *topic*, else a
        fallback); the hex suffix guarantees uniqueness across repeated calls
        with the same slug. Two distinct lessons must never collide on RRF's
        ``(document_name, chunk_index, page_number)`` dedup key -- colliding
        would silently merge two unrelated lessons' scores and drop one
        lesson's text from display.
        """
        base = name or topic or cls._FALLBACK_SLUG
        return f"lesson-{cls._slugify(base)}-{uuid4().hex[:8]}"

    @classmethod
    def _slugify(cls, text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug[: cls._MAX_SLUG_LEN].strip("-") or cls._FALLBACK_SLUG


@final
class LessonsCollection:
    """A project's lessons collection name, derived like :class:`CapturesCollection`."""

    _LESSONS_SUFFIX = "-lessons"
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
        """Return the lessons collection for a project's base collection name."""
        return cls(f"{repo}{cls._LESSONS_SUFFIX}")

    @classmethod
    def resolve(cls, base_collection: str | None) -> Self:
        """Return the lessons collection for *base_collection*, or the fallback."""
        return cls.for_repo(base_collection or cls._FALLBACK_REPO)

    @classmethod
    def for_cwd(cls, cwd: str, registrations: Mapping[str, str]) -> Self:
        """Resolve the lessons collection for *cwd* against the sync registry.

        Shares :meth:`quarry.collection_routing.CollectionRouting.covering_collection`
        with :class:`~quarry.captures_collection.CapturesCollection` -- the
        ancestor walk is identical regardless of the suffix.
        """
        return cls.resolve(CollectionRouting.covering_collection(cwd, registrations))

    @classmethod
    def for_registry_path(cls, cwd: str, registry_path: Path) -> Self:
        """Resolve the lessons collection for *cwd* by reading the sync registry."""
        from quarry.sync_registry import SyncRegistry  # noqa: PLC0415

        conn = SyncRegistry(registry_path)
        try:
            registrations = {
                r.directory: r.collection for r in conn.list_registrations()
            }
        finally:
            conn.close()
        return cls.for_cwd(cwd, registrations)
