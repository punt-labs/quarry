"""Result value objects for the § 2.3 CLAUDE.md/.gitignore enable-disable steps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quarry.skills_install import SkillOutcome

__all__ = ["DisablementResult", "EnablementResult"]


@dataclass(frozen=True, slots=True)
class EnablementResult:
    """What the § 2.3 enable steps did to a repo's CLAUDE.md and .gitignore."""

    guide_deposited: bool
    enabled_marker_written: bool
    import_registered: bool
    gitignore_ensured: bool
    # Cross-harness skill deposits: every SkillOutcome from the
    # best-effort install_detected() call, or () with no source tree of our own.
    skills_deposited: tuple[SkillOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class DisablementResult:
    """What the § 2.3 disable steps did — the guide is left dormant (§ 2.9)."""

    import_pruned: bool
    enabled_marker_removed: bool
