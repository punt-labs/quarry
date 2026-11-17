"""What a repair will drop and how much space it will reclaim."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, final

MIB = 2**20


@final
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

    # The ratio a healthy table awaiting compaction stays far under (the same
    # bar ``quarry doctor`` warns at), and the smallest leak worth a rebuild.
    MAX_RATIO: ClassVar[float] = 10.0
    MIN_RECLAIM_BYTES: ClassVar[int] = MIB

    @property
    def kept_rows(self) -> int:
        """Return how many rows survive the repair."""
        return self.rows - self.binary_rows - self.over_budget_rows

    @property
    def dropped_rows(self) -> int:
        """Return how many rows the repair deletes."""
        return self.binary_rows + self.over_budget_rows

    @property
    def reclaimable_bytes(self) -> int:
        """Return the bytes a rebuild frees beyond the live data."""
        return max(self.on_disk_bytes - self.live_bytes, 0)

    @property
    def is_bloated(self) -> bool:
        """Return whether leaked files dwarf the live data."""
        return (
            self.reclaimable_bytes >= self.MIN_RECLAIM_BYTES
            and self.on_disk_bytes > self.MAX_RATIO * max(self.live_bytes, 1)
        )

    @property
    def needs_repair(self) -> bool:
        """Return whether a rebuild would change anything worth consenting to."""
        return self.dropped_rows > 0 or self.is_bloated

    def describe(self) -> str:
        """Return the summary an operator reads before consenting."""
        lines = [
            (
                f"{self.rows} rows: {self.binary_rows} binary and "
                f"{self.over_budget_rows} over-budget rows will be dropped, "
                f"{self.kept_rows} kept."
            ),
            (
                f"live data {self.live_bytes / MIB:.1f} MiB, "
                f"on disk {self.on_disk_bytes / MIB:.1f} MiB"
            ),
        ]
        if self.is_bloated:
            lines.append(
                f"a rebuild will also reclaim {self.reclaimable_bytes / MIB:.1f} MiB "
                "of leaked files"
            )
        return "\n".join(lines)
