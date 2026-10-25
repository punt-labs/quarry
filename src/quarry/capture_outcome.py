"""How a capture write ended, each outcome with the reason a log line gives."""

from __future__ import annotations

from enum import Enum


class CaptureOutcome(Enum):
    """The ways a capture file write can end; the value is the reason, in words."""

    WRITTEN = "capture file written"
    EMPTY = "nothing to capture"
    SYMLINK = "capture refused: symlink"
    FAILED = "capture file could not be written"
