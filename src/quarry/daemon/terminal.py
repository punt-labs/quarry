"""Record a background task's terminal status when its body exits."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self, final

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.task_lifecycle import TaskLifecycle
from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from quarry.daemon.lifecycle import Lifecycle
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class Terminal:
    """Bind a task's state to the lifecycle that settles it as its body exits.

    A queued job ends through :class:`~quarry.daemon.job_lifecycle.JobLifecycle`,
    which never overwrites a status its producer or its store settled; any other
    polled task (a sync, a delete, a registration, a backfill) ends through
    :class:`~quarry.daemon.task_lifecycle.TaskLifecycle`.  The recording is the
    same either way.
    """

    __slots__ = ("_lifecycle", "_state")

    _lifecycle: Lifecycle
    _state: TaskState

    def __new__(cls, state: TaskState, lifecycle: Lifecycle) -> Self:
        self = super().__new__(cls)
        self._state = state
        self._lifecycle = lifecycle
        return self

    @classmethod
    def for_job(cls, state: TaskState) -> Self:
        """Return the terminal of a queued job's body."""
        return cls(state, JobLifecycle(state))

    @classmethod
    def for_task(cls, state: TaskState) -> Self:
        """Return the terminal of a polled task that is not a queued job."""
        return cls(state, TaskLifecycle(state))

    @property
    def kind(self) -> str:
        """Return the kind of task this terminal records."""
        return self._state.kind

    @property
    def task_id(self) -> str:
        """Return the id of the task this terminal records."""
        return self._state.task_id

    def settle_stored(self, results: Mapping[str, object]) -> bool:
        """Complete the task with *results*; return whether it was."""
        return self._lifecycle.settle_stored(results)

    def settle_failed(self, reason: str) -> bool:
        """Fail the task with *reason*; return whether it was."""
        return self._lifecycle.settle_failed(reason)

    @contextmanager
    def recording(self) -> Generator[None]:
        """Record the task's terminal status when its body exits.

        Cancellation is recorded and re-raised; any other exception is logged
        once, by class and frames, and recorded as ``FailureReason.of`` words it:
        a refusal's text, else ``internal error: <Class>`` (a ReportedError's
        carried class); a body that sets no terminal status fails.
        """
        try:
            yield
        except asyncio.CancelledError:
            self._lifecycle.cancelled("task was cancelled")
            raise
        except Exception as exc:
            logger.error(
                "Background %s failed",
                self._state.kind,
                exc_info=RedactedFailure.of(exc),
            )
            self._lifecycle.settle_failed(FailureReason.of(exc))
        finally:
            self._lifecycle.exited("task exited without setting terminal status")
