"""How a scan's outcome reaches the task that ran it."""

from __future__ import annotations

from typing import Protocol


class ScanReport[T](Protocol):
    """Where a scan's outcome is written, on time, late, and part-way."""

    def record(self, result: T) -> None:
        """Write *result*, the scan's outcome, to the task."""
        ...

    def late(self, result: T) -> None:
        """Write the outcome of a scan that returned after the task gave up on it."""
        ...

    def so_far(self) -> T:
        """Return what the scan has counted so far, while it still runs."""
        ...
