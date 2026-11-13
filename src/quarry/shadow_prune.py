"""The outcome of one attempt to strip a stray user-scope MCP shadow entry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from quarry.results import CheckResult

_CHECK_NAME = "User-scope MCP shadow"


class ShadowPruneKind(Enum):
    """How one :class:`~quarry.doctor_mcp.UserScopeShadow.prune` attempt resolved."""

    # The stray key was found and stripped.
    REMOVED = auto()
    # A genuine no-op: the plugin is absent, the file is absent, or the key
    # is already gone. Never touched the file.
    ABSENT = auto()
    # The file exists but could not be checked (too large to read safely, or
    # not valid JSON) -- distinct from ABSENT so a user is never told "clean"
    # when the entry's presence was never actually verified.
    UNREADABLE = auto()
    # A user-scope quarry entry is present but is NOT the one quarry writes
    # (`quarry mcp`) -- a deliberate custom command or remote the user set.
    # Left untouched: deleting it would be silent data loss.
    PRESERVED = auto()


@dataclass(frozen=True, slots=True)
class ShadowPrune:
    """One prune outcome: what happened, and why."""

    kind: ShadowPruneKind
    detail: str = ""

    def message(self) -> str:
        """Return the ``quarry doctor`` report line for this outcome."""
        if self.kind is ShadowPruneKind.REMOVED:
            return "removed stray user-scope entry"
        if self.kind is ShadowPruneKind.ABSENT:
            return "nothing to prune"
        if self.kind is ShadowPruneKind.PRESERVED:
            return (
                "a custom user-scope 'quarry' MCP entry is set in ~/.claude.json; "
                "left as-is -- remove it by hand if it is stale "
                "('claude mcp remove quarry --scope user')"
            )
        return f"could not check ~/.claude.json for a stray entry ({self.detail})"

    def as_check_result(self) -> CheckResult:
        """Return the ``quarry doctor`` entry for this outcome.

        Always ``passed=True`` -- an unreadable ``~/.claude.json`` says so in
        its own message rather than failing the check, since the shadow entry
        (if any) is cosmetic, not a broken install.
        """
        return CheckResult(
            name=_CHECK_NAME, passed=True, message=self.message(), required=False
        )
