"""The value types on either side of a mission-memory sync."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Iterable


@final
@dataclass(frozen=True, slots=True)
class SyncOptions:
    """What the caller asked for: one mission or all, a rehearsal, a re-file."""

    mission_id: str = ""
    dry_run: bool = False
    force: bool = False


@final
@dataclass(frozen=True, slots=True)
class MissionSyncOutcome:
    """What a sync did, by document name, plus every error it kept going past."""

    filed: tuple[str, ...]
    skipped: tuple[str, ...]
    errors: tuple[str, ...]
    dry_run: bool

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict[str, object]:
        """Return the ``--json`` shape."""
        return {
            "filed": list(self.filed),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "dry_run": self.dry_run,
        }

    def render(self) -> str:
        """Return the human summary, one line per document and per error."""
        verb = "would file" if self.dry_run else "filed"
        lines = [
            f"▶  Mission memories: {verb} {len(self.filed)}, "
            f"skipped {len(self.skipped)}, errors {len(self.errors)}"
        ]
        lines.extend(f"  {verb}: {name}" for name in self.filed)
        lines.extend(f"  skipped (already filed): {name}" for name in self.skipped)
        lines.extend(f"  error: {error}" for error in self.errors)
        return "\n".join(lines)


@final
class SyncTally:
    """Accumulate one sync's dispositions; ``outcome()`` freezes them.

    Mutable on purpose (a Builder): the sync adds one line per round and keeps
    going past every failure, so a daemon error on the Nth round can never
    discard the rounds already filed before it.
    """

    __slots__ = ("_errors", "_filed", "_skipped")

    _filed: list[str]
    _skipped: list[str]
    _errors: list[str]

    def __new__(cls, errors: Iterable[str] = ()) -> Self:
        self = super().__new__(cls)
        self._filed = []
        self._skipped = []
        self._errors = list(errors)
        return self

    def record_filed(self, name: str) -> None:
        self._filed.append(name)

    def record_skipped(self, name: str) -> None:
        self._skipped.append(name)

    def record_error(self, message: str) -> None:
        self._errors.append(message)

    def outcome(self, *, dry_run: bool) -> MissionSyncOutcome:
        """Return the frozen outcome, in the order each disposition arrived."""
        return MissionSyncOutcome(
            filed=tuple(self._filed),
            skipped=tuple(self._skipped),
            errors=tuple(self._errors),
            dry_run=dry_run,
        )
