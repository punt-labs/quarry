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
    _LOGGED_KEY: ClassVar[str] = "quarry_logged"  # on the exception's own __dict__

    __slots__ = ()

    @classmethod
    def of(cls, exc: BaseException) -> ReportedError:
        """Return the stand-in for *exc*, to pass as a record's ``exc_info``.

        An ``OSError`` also names its errno (``OSError [ENOSPC]``): the errno
        carries no path, and it is what tells a full disk from a denied write.
        The cause (or the exception being handled when *exc* was raised, unless
        the raise suppressed it) gets a stand-in of its own, chained, so the root
        failure survives a cleanup that raised another.  A chain past
        :attr:`MAX_CHAIN`, or one that loops, ends with a stand-in that says so.
        """
        return cls._chain(exc, 0, frozenset())

    @classmethod
    def mark_logged(cls, exc: BaseException) -> None:
        """Note that *exc* was logged, so a later boundary does not log it again."""
        vars(exc)[cls._LOGGED_KEY] = True

    @classmethod
    def is_logged(cls, exc: BaseException) -> bool:
        """Return whether :meth:`mark_logged` was called for *exc*."""
        return bool(vars(exc).get(cls._LOGGED_KEY))

    @classmethod
    def _chain(
        cls, exc: BaseException, depth: int, seen: frozenset[int]
    ) -> ReportedError:
        """Return *exc*'s stand-in, linked to its origin's, *depth* links in."""
        standin = ReportedError(cls._name(exc)).with_traceback(exc.__traceback__)
        origin = exc.__cause__
        if origin is None and not exc.__suppress_context__:
            origin = exc.__context__
        if origin is None:
            return standin
        if depth >= cls.MAX_CHAIN or id(origin) in seen | {id(exc)}:
            standin.__cause__ = ReportedError(
                f"chain truncated after {depth + 1} links"
            )
        else:
            standin.__cause__ = cls._chain(origin, depth + 1, seen | {id(exc)})
        return standin

    @staticmethod
    def _name(exc: BaseException) -> str:
        """Return *exc*'s class name, with its errno name for an ``OSError``."""
        name = type(exc).__name__
        if isinstance(exc, OSError) and exc.errno:
            return f"{name} [{FailureReason.os_name(exc)}]"
        return name
