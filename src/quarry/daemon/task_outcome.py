"""Record how a background job ended, treating a refusal as an outcome, not a fault."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Self, final

from quarry.content_guard import UnindexableContentError
from quarry.daemon.tasks import task_terminal

if TYPE_CHECKING:
    from collections.abc import Generator

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class TaskOutcome:
    """Bind a task's state to the way its body is allowed to end.

    :func:`task_terminal` logs every exception with a traceback.  Content the
    chunker refuses (binary that slipped past the request sniff) is an expected
    answer, so it ends the task ``failed`` with the reason and one WARN, no
    traceback; any other exception still goes to :func:`task_terminal` unchanged.
    """

    __slots__ = ("_state",)

    _state: TaskState

    def __new__(cls, state: TaskState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    @contextmanager
    def record(self) -> Generator[None]:
        """Run the body, recording the task's terminal state when it exits."""
        with task_terminal(self._state):
            try:
                yield
            except UnindexableContentError as exc:
                logger.warning("Background %s rejected: %s", self._state.kind, exc)
                self._state.status = "failed"
                self._state.error = str(exc)
