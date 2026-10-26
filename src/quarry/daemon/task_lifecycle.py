"""A polled background task's status moves: it is running until it is told otherwise."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self, final

if TYPE_CHECKING:
    from collections.abc import Mapping

    from quarry.daemon.tasks import TaskState


class Settles(Protocol):
    """The moves a terminal makes when its body says how it ended."""

    def settle_stored(self, results: Mapping[str, object]) -> bool:
        """Complete the task with *results*; return whether it was."""
        ...

    def settle_failed(self, reason: str) -> bool:
        """Fail the task with *reason*; return whether it was."""
        ...


@final
class TaskLifecycle:
    """The status moves of a task that is not a queued job.

    A sync, a delete, a registration or a backfill is ``running`` until it is
    ``completed`` or ``failed``, and nothing else withdraws, commits or stores
    behind its back, so what its body says last stands: a task that recorded a
    partial result and is then cancelled is ``failed``, and a body that ends with
    no terminal status failed.  A queued job, which a producer can withdraw and
    whose store outlives its worker, has its own rules
    (:class:`~quarry.daemon.job_lifecycle.JobLifecycle`).  Every move holds the
    task's claim.
    """

    __slots__ = ("_state",)

    _state: TaskState

    def __new__(cls, state: TaskState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    def settle_stored(self, results: Mapping[str, object]) -> bool:
        """Complete the task with *results*; always done, so always ``True``."""
        with self._state.claim:
            self._state.status = "completed"
            self._state.error = ""
            self._state.results = dict(results)
        return True

    def settle_failed(self, reason: str) -> bool:
        """Fail the task with *reason*; always done, so always ``True``."""
        with self._state.claim:
            self._state.status = "failed"
            self._state.error = reason
        return True

    def cancelled(self, default: str) -> None:
        """Fail the task whatever it had recorded, keeping a reason it gave."""
        with self._state.claim:
            self._fail(default)

    def exited(self, default: str) -> None:
        """Fail a task whose body ended with neither ``completed`` nor ``failed``."""
        with self._state.claim:
            if self._state.status not in {"completed", "failed"}:
                self._fail(default)

    def _fail(self, default: str) -> None:
        """Fail with the reason the task gave, else *default* (claim held)."""
        self._state.status = "failed"
        self._state.error = self._state.reason_or(default)
