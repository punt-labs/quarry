"""The client-visible reason a unit of work failed, and which failures are per-file."""

from __future__ import annotations

import sqlite3
import traceback
from typing import ClassVar, final

from quarry.refusal import RefusalError
from quarry.reported_error import ReportedError


@final
class FailureReason:
    """Fail closed on what a failure tells a client.

    A per-file error reaches a client through a task result, a progress line and
    the sync summary.  An arbitrary exception's message can quote a path, a URL
    or a credential, so only its class is shown, and the detail goes to the log
    (redacted).  A :class:`~quarry.refusal.RefusalError`, whose message is a
    stable, URL-free reason by construction, keeps it.

    :attr:`RECOVERABLE` is the set of failures one file may raise without
    stopping the surrounding sync or watch job: a vanished or unreadable file, an
    unsupported format, a registry error.
    """

    __slots__ = ()

    RECOVERABLE: ClassVar[tuple[type[Exception], ...]] = (
        sqlite3.Error,
        OSError,
        ValueError,
        RuntimeError,
        TimeoutError,
    )

    @staticmethod
    def of(exc: Exception) -> str:
        """Return a refusal's message, else ``internal error: <Class>``.

        A :class:`ReportedError` names the class of a bug caught elsewhere.
        """
        if isinstance(exc, RefusalError):
            return str(exc)
        if isinstance(exc, ReportedError):
            return f"internal error: {exc.reported_as}"
        return f"internal error: {type(exc).__name__}"

    @staticmethod
    def frames(exc: BaseException) -> str:
        """Return *exc*'s stack frames without its message, safe to log.

        The message is where a URL or a path hides; the frames name only code.
        """
        return "".join(traceback.format_tb(exc.__traceback__))

    @classmethod
    def line(cls, name: str, exc: Exception) -> str:
        """Return the per-file error line ``<name>: <reason>``."""
        return f"{name}: {cls.of(exc)}"
