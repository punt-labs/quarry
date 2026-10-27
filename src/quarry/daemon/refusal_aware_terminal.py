"""Record how a background job ended, treating a refusal as an outcome, not a fault."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.content_guard import UnindexableContentError
from quarry.daemon.settlement import Settlement
from quarry.daemon.terminal import Terminal
from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class RefusalAwareTerminal:
    """Bind a task's state to the way its body is allowed to end.

    :meth:`Terminal.recording` logs every exception with a traceback.  A refusal
    (:class:`RefusalError`: binary content, a refused media type, a failed or
    refused fetch, a refused registration) is an expected answer, so it ends the task
    ``failed`` with the reason and one WARN naming the task, no traceback.  Any
    other exception fails closed: the task error is client-visible and such a
    message can quote a URL, a path or a credential, so :meth:`Terminal.recording`
    records ``internal error: <Class>`` and logs the detail, redacted.  This class
    is the single owner of the refusal write.

    Every content job runs under it.  ``CaptureIngestJob._refetch`` does not
    swallow a refusal: it runs only when the inline phase stored nothing, so a
    refused re-fetch leaves the capture unstored and the task must say so.
    ``subject`` names what was being ingested (a redacted URL or a scrubbed
    name); it is called only when a refusal is logged, so building it can never
    stop the task from reaching a terminal state, and it must be safe to log.
    """

    __slots__ = ("_subject", "_terminal")

    NO_PAGES: ClassVar[str] = "nothing was stored: no pages were selected for ingest"
    NOTHING_STORED: ClassVar[str] = (
        "nothing was stored: the content produced no indexable text"
    )

    _subject: Callable[[], str]
    _terminal: Terminal

    def __new__(cls, terminal: Terminal, subject: Callable[[], str]) -> Self:
        self = super().__new__(cls)
        self._subject = subject
        self._terminal = terminal
        return self

    @classmethod
    def for_job(cls, state: TaskState, subject: Callable[[], str] = str) -> Self:
        """Return the terminal of a queued job's body."""
        return cls(Terminal.for_job(state), subject)

    @classmethod
    def for_task(cls, state: TaskState, subject: Callable[[], str] = str) -> Self:
        """Return the terminal of a polled task that is not a queued job."""
        return cls(Terminal.for_task(state), subject)

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

    def apply(self, settlement: Settlement) -> None:
        """Record how the store ended: completed, or refused with its reason."""
        if settlement.is_stored:
            self._terminal.settle_stored(settlement.results)
        else:
            self._fail(UnindexableContentError(settlement.reason))

    @contextmanager
    def record(self) -> Generator[None]:
        """Run the body, recording the task's terminal state when it exits."""
        with self._terminal.recording():
            try:
                yield
            except RefusalError as exc:
                self._fail(exc)

    def _fail(self, exc: RefusalError) -> None:
        """Record the failure first, then log it: naming the task cannot undo it.

        A failure the task's status refused (the producer withdrew the job, or its
        store already settled it) is not logged as a rejection: it was not one.
        """
        if not self._terminal.settle_failed(str(exc)):
            return
        subject = "(unnamed)"
        # The subject only decorates a log line, so whatever naming it raises must
        # not stop the failure from being logged; it is reported, not swallowed.
        try:
            subject = self._subject()
        except Exception:
            logger.warning(
                "Background %s (task %s): naming the subject failed",
                self._terminal.kind,
                self._terminal.task_id,
                exc_info=True,
            )
        logger.warning(
            "Background %s %s (task %s) rejected: %s",
            self._terminal.kind,
            subject,
            self._terminal.task_id,
            f"{exc} [{exc.detail}]" if exc.detail else exc,
        )
