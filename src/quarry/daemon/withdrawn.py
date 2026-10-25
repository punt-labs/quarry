"""What a producer that stops waiting for a job learns about it."""

from __future__ import annotations

from enum import Enum, auto


class Withdrawn(Enum):
    """Where a job stood when its producer withdrew it.

    ``NEVER_STARTED``: it never ran and now never will.  ``STARTED``: it was
    running but had not committed; it will stop at its commit and write nothing.
    ``COMMITTING``: it is past its commit and will store.  ``STORED`` and
    ``FAILED``: it had already finished, so the answer is its own.
    """

    NEVER_STARTED = auto()
    STARTED = auto()
    COMMITTING = auto()
    STORED = auto()
    FAILED = auto()
