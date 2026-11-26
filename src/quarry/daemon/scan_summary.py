"""Roll the child scans of an explicit sync up into its umbrella task."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.daemon.task_state import TERMINAL_STATUSES

if TYPE_CHECKING:
    from quarry.daemon.task_state import TaskState


@final
class ScanSummary:
    """Track the child scans of one sync and settle its umbrella task from them.

    A ``CollectionSyncJob`` completes even when N files failed (it records
    ``failed``/``errors`` in its own state), so counting only child *status*
    would report silent success.  :meth:`settle` totals both the shed-job count
    and the per-file failure count/errors, and fails the umbrella if either is
    nonzero.

    *collections* is reported by the sweep rather than derived from the child
    count: children are N scans plus one finalize per database, so no fixed
    arithmetic over ``children`` recovers it.
    """

    __slots__ = ("_children", "_collections")

    _children: list[TaskState]
    _collections: int

    def __new__(cls, children: list[TaskState], collections: int) -> Self:
        """Bind the live child task states and the collection count they cover."""
        self = super().__new__(cls)
        self._children = children
        self._collections = collections
        return self

    def pending(self) -> bool:
        """Return whether any child scan has not reached a terminal status."""
        return any(child.status not in TERMINAL_STATUSES for child in self._children)

    def settle(self, umbrella: TaskState, *, timed_out: bool) -> None:
        """Complete or fail *umbrella* from the children's current outcomes."""
        shed = sum(1 for child in self._children if child.status == "failed")
        file_failures = 0
        errors: list[str] = []
        for child in self._children:
            failed = child.results.get("failed", 0)
            if isinstance(failed, int):
                file_failures += failed
            child_errors = child.results.get("errors")
            if isinstance(child_errors, list):
                errors.extend(str(error) for error in child_errors)
        results: dict[str, object] = {
            "collections": self._collections,
            "failed": file_failures,
            "shed": shed,
            "errors": errors,
        }
        if timed_out:
            umbrella.record(results)
            umbrella.fail("scan timed out before all jobs completed")
        elif shed or file_failures:
            umbrella.record(results)
            umbrella.fail(f"{shed} scan job(s) shed, {file_failures} file(s) failed")
        else:
            umbrella.complete(results)
