"""A stand-in for an exception that is safe to hand to ``exc_info``."""

from __future__ import annotations

from typing import final

from quarry.failure_reason import FailureReason
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
        """Return the stand-in for *exc*, to pass as a record's ``exc_info``.

        An ``OSError`` also names its errno (``OSError [ENOSPC]``): the errno
        carries no path, and it is what tells a full disk from a denied write.
        """
        name = type(exc).__name__
        if isinstance(exc, OSError) and exc.errno:
            name = f"{name} [{FailureReason.os_name(exc)}]"
        return ReportedError(name).with_traceback(exc.__traceback__)
