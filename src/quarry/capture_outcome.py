"""How a capture write ended, each outcome with the reason a log line gives."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import final


class CaptureOutcome(Enum):
    """The ways a capture file write can end; the value is the reason, in words."""

    WRITTEN = "capture file written"
    EMPTY = "nothing to capture"
    SYMLINK = "capture refused: symlink or non-directory in path"
    FAILED = "capture file could not be written"


@final
@dataclass(frozen=True, slots=True)
class CaptureResult:
    """How a capture write ended, and for a failure what caused it.

    *cause* is the failure's errno name or class, never its message, so it is
    safe to put in a log line or a run's error line; it is ``""`` for every
    outcome but ``FAILED``.
    """

    outcome: CaptureOutcome
    cause: str = ""
