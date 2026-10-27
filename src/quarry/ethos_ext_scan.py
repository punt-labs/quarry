"""The outcome vocabulary of one refresh pass over an ethos identities tree."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import final


class ExtWriteResult(StrEnum):
    """What the guide writer did to one identity's ``quarry.yaml``."""

    UPDATED = "updated"
    ALREADY_SET = "already_set"
    NO_COLLECTION = "no_collection"


@final
@dataclass(frozen=True, slots=True)
class ExtFailure:
    """One identity whose ext file could not be read or written."""

    handle: str
    reason: str

    def __str__(self) -> str:
        return f"{self.handle}: {self.reason}"


@final
@dataclass(frozen=True, slots=True)
class ExtScanOutcome:
    """Per-handle buckets from one pass over an identities directory.

    ``failed`` carries the handles whose ext raised an I/O, YAML, or decoding
    error — the guide never landed for them, so a caller must not report
    unqualified success.
    """

    updated: tuple[str, ...] = ()
    already_set: tuple[str, ...] = ()
    no_collection: tuple[str, ...] = ()
    failed: tuple[ExtFailure, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Return whether the scan found no identity with quarry configured."""
        return not (
            self.updated or self.already_set or self.no_collection or self.failed
        )

    @property
    def failed_handles(self) -> tuple[str, ...]:
        return tuple(failure.handle for failure in self.failed)

    def message(self) -> str:
        """Render the buckets as the one-line doctor/install message."""
        parts: list[str] = []
        if self.updated:
            noun = "identity" if len(self.updated) == 1 else "identities"
            parts.append(
                f"updated {len(self.updated)} {noun}: {', '.join(self.updated)}"
            )
        if self.already_set:
            prefix = "already set" if self.updated else "session_context already set"
            parts.append(f"{prefix}: {', '.join(self.already_set)}")
        if self.no_collection:
            parts.append(
                f"no memory_collection (check config): {', '.join(self.no_collection)}"
            )
        if self.failed:
            parts.append(f"errors: {'; '.join(str(f) for f in self.failed)}")
        return "; ".join(parts)
