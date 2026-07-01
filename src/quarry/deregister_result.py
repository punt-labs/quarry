"""Typed outcome of a directory deregistration, shared by local and remote paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final


@final
@dataclass(frozen=True, slots=True)
class DeregisterResult:
    """Outcome of removing a directory registration.

    Returned identically by the CLI's local and remote deregister paths so
    remote/local field parity is a compile-time guarantee, not a convention.
    """

    collection: str
    removed: int
    deleted_chunks: int

    @classmethod
    def from_task(cls, collection: str, task: dict[str, object]) -> Self:
        """Build from a completed remote purge-task status payload.

        ``task`` is a wire boundary — JSON deserialisation yields ``object``
        values that are coerced to ``int`` here.
        """
        return cls(
            collection,
            cls._as_int(task.get("removed")),
            cls._as_int(task.get("deleted_chunks")),
        )

    def as_dict(self) -> dict[str, object]:
        """Return the JSON payload shape shared by both deregister paths.

        The return is the wire representation emitted under ``--json``; both
        paths must produce these exact keys.
        """
        return {
            "collection": self.collection,
            "removed": self.removed,
            "deleted_chunks": self.deleted_chunks,
        }

    @staticmethod
    def _as_int(value: object) -> int:
        """Coerce a JSON scalar to ``int``; absent or non-numeric maps to 0."""
        return value if isinstance(value, int) and not isinstance(value, bool) else 0
