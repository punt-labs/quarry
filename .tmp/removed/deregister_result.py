"""Typed outcome of a directory deregistration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True, slots=True)
class DeregisterResult:
    """Outcome of removing a directory registration — the ``--json`` shape."""

    collection: str
    removed: int
    deleted_chunks: int

    def as_dict(self) -> dict[str, object]:
        """Return the JSON payload the deregister command emits."""
        return {
            "collection": self.collection,
            "removed": self.removed,
            "deleted_chunks": self.deleted_chunks,
        }
