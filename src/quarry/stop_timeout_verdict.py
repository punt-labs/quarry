"""What reading an installed service's stop timeout came to."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from pathlib import Path


class StopTimeoutState(Enum):
    """What reading an installed service's stop timeout came to."""

    NOT_INSTALLED = auto()  # no service file: nothing to check
    UNREADABLE = auto()  # the file is there and cannot be read
    UNPARSEABLE = auto()  # it was read and is not a plist or a unit
    UNRECOGNIZED = auto()  # a stop timeout value this cannot interpret
    INFINITE = auto()  # systemd ``infinity``: never short
    SHORT = auto()  # missing, or below what the teardown needs
    OK = auto()


_PROBLEMS = frozenset(
    {
        StopTimeoutState.UNREADABLE,
        StopTimeoutState.UNPARSEABLE,
        StopTimeoutState.UNRECOGNIZED,
        StopTimeoutState.SHORT,
    }
)


@final
@dataclass(frozen=True, slots=True)
class StopTimeoutVerdict:
    """The state of an installed stop timeout, with what explains it.

    ``detail`` is the errno name (unreadable), the exception class (unparseable)
    or the raw value (unrecognized).  ``value`` is the stop timeout read, in
    seconds, ``None`` when the file carries none; ``needed`` is what the policy
    requires.
    """

    state: StopTimeoutState
    path: Path
    detail: str = ""
    value: float | None = None
    needed: int = 0

    @property
    def is_problem(self) -> bool:
        """Return whether this verdict is something to tell the operator."""
        return self.state in _PROBLEMS

    @property
    def message(self) -> str:
        """Return the verdict in words, naming the file and, for a problem, the fix."""
        fix = "run 'quarry install' to rewrite it"
        match self.state:
            case StopTimeoutState.NOT_INSTALLED:
                return "no installed service; not checked"
            case StopTimeoutState.UNREADABLE:
                what = f"cannot be read ({self.detail}); its stop timeout is unchecked"
            case StopTimeoutState.UNPARSEABLE:
                what = (
                    f"cannot be parsed ({self.detail}); its stop timeout is unchecked"
                )
            case StopTimeoutState.UNRECOGNIZED:
                what = f"has a stop timeout this check cannot interpret ({self.detail})"
            case StopTimeoutState.INFINITE:
                return f"{self.path} never kills the daemon (stop timeout infinite)"
            case StopTimeoutState.SHORT:
                have = "has none" if self.value is None else f"is {self.value:g}s"
                what = (
                    f"stop timeout {have}, below the {self.needed}s the daemon's "
                    "shutdown needs, so the service manager may kill it mid-drain"
                )
            case StopTimeoutState.OK:
                return f"{self.path} waits {self.value:g}s for the daemon to stop"
        return f"the installed service file {self.path} {what}; {fix}"
