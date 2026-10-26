"""How a job's store ended, in the words a job's status needs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping


@final
@dataclass(frozen=True, slots=True)
class Settlement:
    """What a finished store means for its job: it stored (with *results*), or failed.

    A job supplies the rule that turns its store's raw result into one of these
    (zero rows is a failure, an index job's error is its own), and the same rule
    settles a store whose job still waits for it and one whose job was cancelled.
    """

    is_stored: bool
    results: Mapping[str, object]
    reason: str

    @classmethod
    def done(cls, results: Mapping[str, object]) -> Self:
        """Return the settlement of a store that wrote what the job asked."""
        return cls(is_stored=True, results=results, reason="")

    @classmethod
    def failed(cls, reason: str) -> Self:
        """Return the settlement of a store that wrote nothing, for *reason*."""
        return cls(is_stored=False, results={}, reason=reason)
