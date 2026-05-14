"""Collection name value class with Flyweight caching."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Self, cast, final
from weakref import WeakValueDictionary


@final
class CollectionName:
    """Validated, cached collection name.

    Flyweight pattern: identical names yield the same object instance.
    Validation happens once at construction time.
    """

    _cache: ClassVar[WeakValueDictionary[str, CollectionName]] = WeakValueDictionary()

    __slots__ = ("__weakref__", "_name")

    _name: str

    def __new__(cls, name: str) -> Self:
        """Create or retrieve a validated CollectionName."""
        name = name.strip()
        if not name:
            msg = "Collection name must not be empty"
            raise ValueError(msg)
        if "'" in name:
            msg = f"Collection name must not contain single quotes: {name!r}"
            raise ValueError(msg)
        if name in cls._cache:
            return cast("Self", cls._cache[name])  # type: ignore[redundant-cast]  # pyright needs this
        self = super().__new__(cls)
        self._name = name
        cls._cache[name] = self
        return self

    @classmethod
    def from_path(
        cls,
        file_path: Path,
        explicit: str | None = None,
    ) -> CollectionName:
        """Derive a collection name from a file path or explicit override."""
        if explicit is not None:
            return cls(explicit)
        return cls(file_path.resolve().parent.name)

    @property
    def name(self) -> str:
        """Return the validated name string."""
        return self._name

    def __str__(self) -> str:
        return self._name

    def __repr__(self) -> str:
        return f"CollectionName({self._name!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CollectionName):
            return NotImplemented
        return self._name == other._name

    def __hash__(self) -> int:
        return hash((CollectionName, self._name))
