"""What a repair will drop and how much space it will reclaim."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RepairPlan:
    """The counts an operator consents to before a repair deletes anything.

    ``live_bytes`` is the size of the rows that survive (text plus vectors);
    ``on_disk_bytes`` is the whole table directory, leaked data files included.
    """

    rows: int
    binary_rows: int
    over_budget_rows: int
    live_bytes: int
    on_disk_bytes: int

    @property
    def kept_rows(self) -> int:
        """Return how many rows survive the repair."""
        return self.rows - self.binary_rows - self.over_budget_rows

    @property
    def dropped_rows(self) -> int:
        """Return how many rows the repair deletes."""
        return self.binary_rows + self.over_budget_rows
