"""Derive a collection name for a directory that collides with no taken name."""

from __future__ import annotations

import hashlib
import re
from itertools import count
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterator, Set as AbstractSet
    from pathlib import Path


@final
class CollectionNamer:
    """Pick a unique collection name for a directory, avoiding every taken name.

    ``taken`` is the union of every name a new registration must steer clear of —
    live registrations, retained (keep-data) archives, and chunk-bearing
    collections.  Both surfaces build that set from their own source (the remote
    client from the wire ``RegistrationList``; the local hooks resolver from the
    registry + catalog) and delegate here, so the two pick identically for the
    same state (remote/local parity), and a different directory can never be
    auto-assigned a name that already holds another project's chunks.
    """

    __slots__ = ("_directory", "_taken")

    _directory: Path
    _taken: AbstractSet[str]

    def __new__(cls, directory: Path, taken: AbstractSet[str]) -> Self:
        self = super().__new__(cls)
        self._directory = directory
        self._taken = taken
        return self

    @classmethod
    def safe(cls, text: str) -> str:
        """Return *text* with every character a name may not hold turned into ``-``."""
        return re.sub(r"[^A-Za-z0-9._-]", "-", text)

    def unique(self) -> str:
        """Return the first candidate name that is not taken."""
        return next(n for n in self._names(self._directory) if n not in self._taken)

    @classmethod
    def _names(cls, directory: Path) -> Iterator[str]:
        """Yield names in order of preference, forever.

        ``taken`` is finite and the sequence ends in an unbounded counter, so a free
        name always comes, and ``unique`` can never return a taken one.

        The directory's leaf name; then ``leaf-parent``; then a lengthening
        path-hash suffix; and finally a numeric counter on the full digest, for a
        digest that is (cryptographically impossibly) taken too.  A filesystem-root
        directory has an empty ``.name``, so the leaf falls back to ``"root"`` — a
        collection is never named the empty string.
        """
        yield (leaf := cls.safe(directory.name) or "root")
        yield f"{leaf}-{cls.safe(directory.parent.name) or 'root'}"
        digest = hashlib.sha256(str(directory).encode()).hexdigest()
        for length in range(8, len(digest) + 1):
            yield f"{leaf}-{digest[:length]}"
        for n in count(2):
            yield f"{leaf}-{digest}-{n}"
