"""The run of transcripts in a row that a full queue would not take."""

from __future__ import annotations

from typing import ClassVar, Self, final


@final
class AdmissionStreak:
    """Count consecutive "queue stayed full" answers, and say when to give up.

    After :attr:`MAX` in a row the queue is not draining, and each further
    transcript would wait out the same deadline for the same answer.  Any other
    answer from the queue ends the streak.
    """

    MAX: ClassVar[int] = 3

    __slots__ = ("_count",)

    _count: int

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._count = 0
        return self

    def answered(self) -> None:
        """End the streak: the queue took or answered a transcript."""
        self._count = 0

    def refused(self) -> bool:
        """Extend the streak by a transcript the queue would not take.

        Returns whether it is now long enough that the run should give up.
        """
        self._count += 1
        return self._count >= self.MAX
