"""The typed terminal outcome of a polled background task.

``RemoteClient.await_task`` printed a hardcoded ``"Deregister failed"`` for every
202 caller; :class:`TaskOutcome` replaces that with an operation-agnostic value
object.  ``QuarryClient.await_task`` returns one of these and the command layer
decides the wording per operation, so an ingest or optimize wait never renders a
deregister message.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Self, final

# ``completed``/``failed`` are wire states; ``timed_out``/``unreachable`` are
# client-side poll conditions the wire ``TaskStatus`` cannot express.
TerminalStatus = Literal["completed", "failed", "timed_out", "unreachable"]


@final
@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """The terminal result of awaiting a 202 task — never names an operation."""

    _task_id: str
    _status: TerminalStatus
    # wire boundary — the operation's own result dict, populated only on success.
    _results: Mapping[str, object]
    _error: str

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def status(self) -> TerminalStatus:
        return self._status

    @property
    def results(self) -> Mapping[str, object]:
        # Empty unless the task completed; the caller reads its operation's keys.
        return self._results

    @property
    def error(self) -> str:
        # Empty unless the task failed or the server became unreachable.
        return self._error

    def result_int(self, key: str) -> int:
        """Return ``results[key]`` when it is a non-bool int, else 0 (a wire count)."""
        value = self._results.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @property
    def is_completed(self) -> bool:
        return self._status == "completed"

    @property
    def is_failed(self) -> bool:
        return self._status == "failed"

    @classmethod
    def completed(cls, task_id: str, results: Mapping[str, object]) -> Self:
        """Build a completed outcome carrying the operation's result dict."""
        return cls(task_id, "completed", results, "")

    @classmethod
    def failed(cls, task_id: str, error: str) -> Self:
        """Build a failed outcome carrying the server's task error."""
        return cls(task_id, "failed", {}, error)

    @classmethod
    def timed_out(cls, task_id: str) -> Self:
        """Build an outcome for a task still running when the deadline elapsed."""
        return cls(task_id, "timed_out", {}, "")

    @classmethod
    def unreachable(cls, task_id: str, error: str) -> Self:
        """Build an outcome after repeated connection losses while polling."""
        return cls(task_id, "unreachable", {}, error)
