"""The teardown budget an installed service carries, or why it is unusable."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from pathlib import Path


class BudgetState(Enum):
    """What looking for the budget in an installed definition found."""

    ABSENT = auto()  # the definition does not set it: settings decide
    VALID = auto()
    INVALID = auto()  # it sets something unusable; ``reason`` says what


@final
@dataclass(frozen=True, slots=True)
class InstalledBudget:
    """The budget the installed definition sets, or the reason it cannot be used.

    ``value`` is in seconds and means something only when the state is ``VALID``.
    """

    state: BudgetState
    path: Path
    value: float = 0.0
    reason: str = ""

    @property
    def message(self) -> str:
        """Return the problem in words, naming the file and the fix."""
        return (
            f"the installed service definition {self.path} carries an unusable "
            f"teardown budget ({self.reason}); run 'quarry install' to rewrite it"
        )
