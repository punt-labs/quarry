"""A background task's state: its status vocabulary and the moves between them."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal, Self, final

from quarry.daemon.owner_lock import OwnerLock

if TYPE_CHECKING:
    from collections.abc import Mapping

    from quarry.daemon.owner_lock import Claim
    from quarry.daemon.store_run import StoreRun

# The one status vocabulary of a background task and of a queued job.  ``new``,
# ``queued``, ``running``, ``committing``, ``aborted`` and ``withdrawn`` belong to
# a queued job's life (:class:`~quarry.daemon.job_lifecycle.JobLifecycle`); a
# polled task is ``running`` until it is ``completed`` or ``failed``.
TaskStatus = Literal[
    "new",
    "queued",
    "running",
    "committing",
    "aborted",
    "withdrawn",
    "completed",
    "failed",
]

# Terminal statuses, eligible for GC.  A task in any other status is still live
# and must never be evicted out from under a client polling it, and a job whose
# body exits in one of them did not finish.
TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset({"completed", "failed"})


@final
class TaskState:
    """Tracks the state of an in-progress or completed background task.

    A task reaches ``completed`` or ``failed`` only through :meth:`complete` and
    :meth:`fail`, and nothing outside this class assigns a field.  ``completed``
    carries no reason and ``failed`` always carries one.  :meth:`move_to` steps a
    queued job through the statuses that are not terminal.

    ``holds_thread`` is true while a scan thread this task started is alive: the
    singleton guard counts the task as running until the thread has returned,
    whatever ``status`` says.  ``claim`` is the lock under which the status moves:
    a queued job is run or withdrawn, never both
    (:class:`~quarry.daemon.job_lifecycle.JobLifecycle`); any other task is only
    ``running``, ``completed`` or ``failed``
    (:class:`~quarry.daemon.task_lifecycle.TaskLifecycle`).
    """

    __slots__ = (
        "_claim",
        "_created_at",
        "_error",
        "_holds_thread",
        "_kind",
        "_results",
        "_status",
        "_store",
        "_task_id",
    )

    _task_id: str
    _kind: str  # "sync", "ingest", "remember", "delete", "register", "deregister"
    _status: TaskStatus
    _results: dict[str, object]
    _error: str
    _created_at: float
    _holds_thread: bool
    # The job's store, kept so a cancelled worker's store can still be read when
    # it ends.  ``None`` until the job starts one: absence is the contract.
    _store: StoreRun | None
    _claim: Claim

    def __new__(
        cls,
        task_id: str,
        kind: str,
        *,
        status: TaskStatus = "running",
        created_at: float | None = None,  # None: the task is created now
        claim: Claim | None = None,  # None: the task owns a fresh lock
    ) -> Self:
        """Start a nonterminal task with empty results and optional ownership.

        Args:
            created_at: Monotonic creation time; None samples the clock.
            claim: Shared owner lock; None creates an independent lock.

        Raises:
            ValueError: The initial status is already terminal.
        """
        if status in TERMINAL_STATUSES:
            msg = f"task {task_id} cannot begin {status}: use complete() or fail()"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._task_id = task_id
        self._kind = kind
        self._status = status
        self._results = {}
        self._error = ""
        self._created_at = time.monotonic() if created_at is None else created_at
        self._holds_thread = False
        self._store = None
        self._claim = OwnerLock() if claim is None else claim
        return self

    def __repr__(self) -> str:
        return f"TaskState({self._task_id!r}, {self._kind!r}, status={self._status!r})"

    @property
    def task_id(self) -> str:
        """Return the id a client polls this task by."""
        return self._task_id

    @property
    def kind(self) -> str:
        """Return what the task does: ``sync``, ``ingest``, ``delete``, ..."""
        return self._kind

    @property
    def status(self) -> TaskStatus:
        """Return where the task is in its life."""
        return self._status

    @property
    def results(self) -> dict[str, object]:
        """Return a copy of what the task has recorded so far."""
        return dict(self._results)

    @property
    def error(self) -> str:
        """Return the reason the task gave, empty until it has one."""
        return self._error

    @property
    def created_at(self) -> float:
        """Return the monotonic time the task was created."""
        return self._created_at

    @property
    def holds_thread(self) -> bool:
        """Return whether a scan thread this task started is still alive."""
        return self._holds_thread

    @property
    def store(self) -> StoreRun | None:
        """Return the store the job started, or ``None`` before it has one."""
        return self._store

    @property
    def claim(self) -> Claim:
        """Return the lock under which this task's status moves."""
        return self._claim

    def reason_or(self, default: str) -> str:
        """Return the reason the task has already given, else *default*."""
        return self._error or default

    def complete(self, results: Mapping[str, object]) -> None:
        """End the task ``completed`` with *results* and no failure reason."""
        self._status = "completed"
        self._error = ""
        self._results = dict(results)

    def fail(self, reason: str) -> None:
        """End the task ``failed`` for *reason*, keeping what it recorded.

        Raises:
            ValueError: *reason* is empty; a failure with no reason tells the
                client nothing.
        """
        if not reason:
            msg = f"task {self._task_id}: a failure needs a reason"
            raise ValueError(msg)
        self._status = "failed"
        self._error = reason

    def move_to(self, status: TaskStatus) -> None:
        """Move a queued job to *status*, one that is not terminal.

        Raises:
            ValueError: *status* is ``completed`` or ``failed``.
        """
        if status in TERMINAL_STATUSES:
            msg = f"task {self._task_id}: reach {status} with complete() or fail()"
            raise ValueError(msg)
        self._status = status

    def record(self, results: Mapping[str, object]) -> None:
        """Replace what the task has recorded so far, leaving its status alone."""
        self._results = dict(results)

    def note(self, reason: str) -> None:
        """Replace the reason the task has given, leaving its status alone."""
        self._error = reason

    def attach_store(self, store: StoreRun) -> None:
        """Keep the *store* the job started, to read when its worker ends."""
        self._store = store

    def hold_thread(self) -> None:
        """Say a scan thread this task started is alive."""
        self._holds_thread = True

    def release_thread(self) -> None:
        """Say the scan thread this task started has returned."""
        self._holds_thread = False
