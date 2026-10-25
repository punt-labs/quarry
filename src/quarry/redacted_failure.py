"""A stand-in for an exception that is safe to hand to ``exc_info``."""

from __future__ import annotations

from typing import ClassVar, final

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

    MAX_CHAIN: ClassVar[int] = 4

    __slots__ = ()

    @classmethod
    def of(cls, exc: BaseException, _depth: int = 0) -> ReportedError:
        """Return the stand-in for *exc*, to pass as a record's ``exc_info``.

        An ``OSError`` also names its errno (``OSError [ENOSPC]``): the errno
        carries no path, and it is what tells a full disk from a denied write.
        The cause (or the exception being handled when *exc* was raised) gets a
        stand-in of its own, chained, so the root failure survives a cleanup that
        raised another; the chain is as deep as :attr:`MAX_CHAIN`.
        """
        standin = ReportedError(cls._name(exc)).with_traceback(exc.__traceback__)
        origin = exc.__cause__ or exc.__context__
        if origin is not None and _depth < cls.MAX_CHAIN:
            standin.__cause__ = cls.of(origin, _depth + 1)
        return standin

    @staticmethod
    def _name(exc: BaseException) -> str:
        """Return *exc*'s class name, with its errno name for an ``OSError``."""
        name = type(exc).__name__
        if isinstance(exc, OSError) and exc.errno:
            return f"{name} [{FailureReason.os_name(exc)}]"
        return name
