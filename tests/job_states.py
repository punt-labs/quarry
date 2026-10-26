"""Job states that got where they are by the lifecycle's own moves."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    from quarry.daemon.tasks import TaskStatus


def job_in(status: TaskStatus, *, kind: str = "remember") -> TaskState:
    """Return a queued job's state in *status*, reached through real moves.

    Nothing assigns the status: the job is enqueued, started, committed,
    withdrawn or settled exactly as a worker, a store and a producer do it.
    """
    state = TaskState(task_id=f"{kind}-{status}", kind=kind, status="new")
    life = JobLifecycle(state)
    if status == "new":
        return state
    assert life.enqueue()
    if status == "withdrawn":
        life.withdraw()
    elif status != "queued":
        assert life.start()
        _from_running(life, status)
    assert state.status == status, (state.status, status)
    return state


def _from_running(life: JobLifecycle, status: TaskStatus) -> None:
    """Move a running job on to *status* (``running`` itself is already there)."""
    if status == "committing":
        assert life.commit()
    elif status == "aborted":
        life.withdraw()
    elif status == "completed":
        assert life.settle_stored({"chunks": 1})
    elif status == "failed":
        assert life.settle_failed("it failed")
