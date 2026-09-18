"""The value types on either side of a mission-memory sync."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final


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
