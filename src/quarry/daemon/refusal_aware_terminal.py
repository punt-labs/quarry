"""Record how a background job ended, treating a refusal as an outcome, not a fault."""

from __future__ import annotations

import logging
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Self, final

from quarry.content_guard import UnindexableContentError
from quarry.daemon.tasks import task_terminal

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class RefusalAwareTerminal:
    """Bind a task's state to the way its body is allowed to end.

    :func:`task_terminal` logs every exception with a traceback.  A refusal
    (:class:`UnindexableContentError`: binary content, a refused media type, a
    failed or refused fetch) is an expected answer, so it ends the task
    ``failed`` with the reason and one WARN naming the task, no traceback.  Any
    other exception fails closed: the task error is client-visible and such a
    message can quote a URL, a path or a credential, so :func:`task_terminal`
    records ``internal error: <Class>`` and logs the detail, redacted.  This class
    is the single owner of the refusal write.

    Every content job runs under it.  ``CaptureIngestJob._refetch`` does not
    swallow a refusal: it runs only when the inline phase stored nothing, so a
    refused re-fetch leaves the capture unstored and the task must say so.
    ``subject`` names what was being ingested (a redacted URL or a scrubbed
    name); it is called only when a refusal is logged, so building it can never
    stop the task from reaching a terminal state, and it must be safe to log.
    """

    __slots__ = ("_state", "_subject")

    _state: TaskState
    _subject: Callable[[], str]

    def __new__(cls, state: TaskState, subject: Callable[[], str] = lambda: "") -> Self:
        self = super().__new__(cls)
        self._state = state
        self._subject = subject
        return self

    def complete(self, results: Mapping[str, object]) -> None:
        """Record the task as completed with *results*."""
        self._state.status = "completed"
        self._state.results = dict(results)

    @contextmanager
    def record(self) -> Generator[None]:
        """Run the body, recording the task's terminal state when it exits."""
        with task_terminal(self._state):
            try:
                yield
            except UnindexableContentError as exc:
                self._fail(exc)

    def _fail(self, exc: UnindexableContentError) -> None:
        """Record the failure first, then log it: naming the task cannot undo it."""
        self._state.status = "failed"
        self._state.error = str(exc)
        subject = "(unnamed)"
        # The subject only decorates a log line, so whatever naming it raises must
        # not stop the failure from being logged.
        with suppress(Exception):
            subject = self._subject()
        logger.warning(
            "Background %s %s (task %s) rejected: %s",
            self._state.kind,
            subject,
            self._state.task_id,
            exc,
        )
