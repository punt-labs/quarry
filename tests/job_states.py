"""Job states that got where they are by the lifecycle's own moves."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.tasks import TaskStatus


@final
class JobStates:
    """Build a queued job's state in a given status, through real moves.

    Nothing assigns the status: the job is enqueued, started, committed,
    withdrawn or settled exactly as a worker, a store and a producer do it.
    """

    __slots__ = ("_life", "_state")

    _life: JobLifecycle
    _state: TaskState

    def __new__(cls, kind: str, status: TaskStatus) -> Self:
        self = super().__new__(cls)
        self._state = TaskState(task_id=f"{kind}-{status}", kind=kind, status="new")
        self._life = JobLifecycle(self._state)
        self._reach(status)
        return self

    @classmethod
    def reaching(cls, status: TaskStatus, *, kind: str = "remember") -> TaskState:
        """Return a job's state in *status*."""
        return cls(kind, status).state

    @property
    def state(self) -> TaskState:
        """Return the state the moves have brought to the requested status."""
        return self._state

    def _reach(self, status: TaskStatus) -> None:
        if status == "new":
            return
        self._must("enqueue", done=self._life.enqueue())
        if status == "withdrawn":
            self._life.withdraw()
        elif status != "queued":
            self._must("start", done=self._life.start())
            self._from_running(status)
        self._must(f"reach {status}", done=self._state.status == status)

    def _from_running(self, status: TaskStatus) -> None:
        """Move a running job on to *status* (``running`` itself is already there)."""
        if status == "committing":
            self._must("commit", done=self._life.commit())
        elif status == "aborted":
            self._life.withdraw()
        elif status == "completed":
            self._must("settle_stored", done=self._life.settle_stored({"chunks": 1}))
        elif status == "failed":
            self._must("settle_failed", done=self._life.settle_failed("it failed"))

    @staticmethod
    def _must(move: str, *, done: bool) -> None:
        if not done:
            msg = f"the lifecycle refused to {move}"
            raise AssertionError(msg)
