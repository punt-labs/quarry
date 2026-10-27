"""A unit whose source outlives it, so an abort needs a re-run, not a spool."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RerunnableUnit(Protocol):
    """A unit that is recovered after an abort by running it again.

    ``rerun_hint`` says how: a backfill job reads a transcript that stays on
    disk, so a drain-aborted one is recovered by running the backfill again.
    """

    @property
    def rerun_hint(self) -> str:
        """Return how to recover this unit after an abort (a short imperative)."""
        ...
