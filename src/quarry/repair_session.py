"""The locked handle through which a repair plans and rebuilds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.repair_plan import RepairPlan


@final
class RepairSession:
    """Plan and rebuild a table, valid only while its daemon locks are held.

    The only way to reach these operations is through ``TableRepair.session()``,
    which yields one of these inside the locks and closes it on the way out, so
    a caller can neither start without the locks nor keep going after they are
    released.
    """

    __slots__ = ("_is_open", "_plan", "_rebuild")

    _is_open: bool
    _plan: Callable[[], RepairPlan]
    _rebuild: Callable[[], int]

    def __new__(
        cls, plan: Callable[[], RepairPlan], rebuild: Callable[[], int]
    ) -> Self:
        self = super().__new__(cls)
        self._is_open = True
        self._plan = plan
        self._rebuild = rebuild
        return self

    def close(self) -> None:
        """End the session; later calls raise."""
        self._is_open = False

    def plan(self) -> RepairPlan:
        """Count what a rebuild would drop and keep; change nothing."""
        self._require_open()
        return self._plan()

    def rebuild(self) -> int:
        """Rebuild the table and swap it in; return its new on-disk size."""
        self._require_open()
        return self._rebuild()

    def _require_open(self) -> None:
        if not self._is_open:
            msg = "the repair session has ended; its daemon locks are released"
            raise ValueError(msg)
