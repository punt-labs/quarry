"""A stand-in for an exception that is safe to hand to ``exc_info``."""

from __future__ import annotations

from typing import final

from quarry.reported_error import ReportedError


@final
class RedactedFailure:
    """Log an unexpected failure's class and frames without its message.

    A store's message can quote the stored name, and a traceback prints it.  The
    stand-in names the class and keeps the frames, the same account
    :meth:`FailureReason.frames` gives a log line, so a cleanup boundary stays
    diagnosable without leaking what it was cleaning up.
    """

    __slots__ = ()

    @staticmethod
    def of(exc: BaseException) -> ReportedError:
        """Return the stand-in for *exc*, to pass as a record's ``exc_info``."""
        return ReportedError(type(exc).__name__).with_traceback(exc.__traceback__)
