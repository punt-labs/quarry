"""Record how a background job ended, treating a refusal as an outcome, not a fault."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.content_guard import UnindexableContentError
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.settlement import Settlement
from quarry.daemon.task_terminal import TaskTerminal
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class RefusalAwareTerminal:
    """Bind a task's state to the way its body is allowed to end.

    :meth:`TaskTerminal.recording` logs every exception with a traceback.  A refusal
    (:class:`RefusalError`: binary content, a refused media type, a failed or
    refused fetch, a refused registration) is an expected answer, so it ends the task
    ``failed`` with the reason and one WARN naming the task, no traceback.  Any
    other exception fails closed: the task error is client-visible and such a
    message can quote a URL, a path or a credential, so :meth:`TaskTerminal.recording`
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

    NO_PAGES: ClassVar[str] = "nothing was stored: no pages were selected for ingest"
    NOTHING_STORED: ClassVar[str] = (
        "nothing was stored: the content produced no indexable text"
    )

    _state: TaskState
    _subject: Callable[[], str]

    def __new__(cls, state: TaskState, subject: Callable[[], str] = lambda: "") -> Self:
        self = super().__new__(cls)
        self._state = state
        self._subject = subject
        return self

    @staticmethod
    def is_stored(results: Mapping[str, object]) -> bool:
        """Return whether a job's *results* left something in the store.

        True when rows were written (``chunks``), pages were ingested
        (``ingested``), or every candidate was already stored (``skipped``: the
        text ``"exists"`` of a create-if-absent skip, or a crawl's positive count).
        """
        return any(results.get(key) for key in ("chunks", "ingested", "skipped"))

    @classmethod
    def settlement(cls, results: Mapping[str, object]) -> Settlement:
        """Return how a store with *results* ended: stored, or refused if none stored.

        A job that wrote no rows did not do what was asked, so reporting
        ``completed`` would claim a store that never happened: it fails with a
        fixed reason that follows the result's shape (a crawl selected no pages;
        a single document had no indexable text), the way a refused fetch does.
        """
        if cls.is_stored(results):
            return Settlement.done(results)
        is_blank_crawl = "ingested" not in results or results.get("empty")
        return Settlement.failed(cls.NOTHING_STORED if is_blank_crawl else cls.NO_PAGES)

    def complete(self, results: Mapping[str, object]) -> None:
        """Record the task as completed with *results*, or refused if none stored."""
        self.apply(self.settlement(results))

    def apply(self, settlement: Settlement) -> None:
        """Record how the store ended: completed, or refused with its reason."""
        if settlement.is_stored:
            JobLifecycle(self._state).settle_stored(settlement.results)
        else:
            self._fail(UnindexableContentError(settlement.reason))

    @contextmanager
    def record(self) -> Generator[None]:
        """Run the body, recording the task's terminal state when it exits."""
        with TaskTerminal.recording(self._state):
            try:
                yield
            except RefusalError as exc:
                self._fail(exc)

    def _fail(self, exc: RefusalError) -> None:
        """Record the failure first, then log it: naming the task cannot undo it."""
        JobLifecycle(self._state).settle_failed(str(exc))
        subject = "(unnamed)"
        # The subject only decorates a log line, so whatever naming it raises must
        # not stop the failure from being logged; it is reported, not swallowed.
        try:
            subject = self._subject()
        except Exception:
            logger.warning(
                "Background %s (task %s): naming the subject failed",
                self._state.kind,
                self._state.task_id,
                exc_info=True,
            )
        logger.warning(
            "Background %s %s (task %s) rejected: %s",
            self._state.kind,
            subject,
            self._state.task_id,
            f"{exc} [{exc.detail}]" if exc.detail else exc,
        )
