"""Say, in one line, why a check could not run."""

from __future__ import annotations

import errno
import logging
import re
from typing import ClassVar, final

from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure

logger = logging.getLogger(__name__)


@final
class CheckFailure:
    """Describe the exception that stopped a check, safely, for a report line.

    A broken native stack can fail an import or a call in several ways, so a
    check that guards one of them catches :attr:`BROKEN_STACK`.  The description
    is the exception's class and its reason: an ``OSError`` is shown by errno
    name only (its message quotes paths), anything else by the first line of its
    message with control characters removed and the length bounded, so a hostile
    or multi-line message cannot spoil the report.
    """

    # What a damaged install raises while importing or calling native code.
    BROKEN_STACK: ClassVar[tuple[type[Exception], ...]] = (
        ImportError,
        OSError,
        AttributeError,
        ValueError,
        RuntimeError,
    )
    _MAX_LINE: ClassVar[int] = 100
    _CONTROL: ClassVar[re.Pattern[str]] = re.compile(r"[^\x20-\x7e]")

    __slots__ = ()

    @classmethod
    def detail(cls, exc: BaseException) -> str:
        """Return ``Class: reason`` (just the class if there is no reason)."""
        name = type(exc).__name__
        if isinstance(exc, OSError):
            errno_name = FailureReason.os_name(exc)
            return name if errno_name == name else f"{name}: {errno_name}"
        text = cls.line(cls._text(exc))
        return f"{name}: {text}" if text else name

    @staticmethod
    def is_missing(exc: OSError | ValueError) -> bool:
        """Return whether *exc* means "not there" rather than "cannot be read".

        A ``ValueError``, or an ``OSError`` with no errno or ``ENOENT``, is a thing
        that is not there; any other errno (denied, I/O) is a real fault.
        """
        return isinstance(exc, ValueError) or exc.errno in (None, errno.ENOENT)

    @classmethod
    def line(cls, text: str) -> str:
        """Return the first line of *text*, printable and bounded."""
        stripped = text.strip()
        first = stripped.splitlines()[0] if stripped else ""
        return cls._CONTROL.sub("?", first)[: cls._MAX_LINE]

    @classmethod
    def origin(cls, exc: BaseException) -> str | None:
        """Return the module the exception was raised in, if its frames say."""
        tb = exc.__traceback__
        last = None
        while tb is not None:
            last, tb = tb, tb.tb_next
        name = last.tb_frame.f_globals.get("__name__") if last else None
        return cls.line(name) if isinstance(name, str) and name else None

    @classmethod
    def import_message(cls, attempted: str, exc: Exception) -> str:
        """Say which module failed to load and why.

        An ``ImportError`` names the module that really failed; any other
        failure names what was being imported and, from its frames, the module it
        was raised in.  Reinstalling is advised only when the module is simply
        missing.
        """
        raw = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
        if isinstance(raw, str) and raw:
            message = f"cannot load {cls.line(raw)} ({cls.detail(exc)})"
        else:
            where = cls.origin(exc)
            at = f" in {where}" if where else ""
            message = f"cannot load {attempted} ({cls.detail(exc)}{at})"
        if isinstance(exc, ModuleNotFoundError):
            return f"{message}; reinstall quarry"
        return message

    @staticmethod
    def _text(exc: BaseException) -> str:
        """Return ``str(exc)`` once; a ``__str__`` that raises reads as empty."""
        try:
            return str(exc)
        except Exception as inner:
            logger.warning(
                "doctor: an exception's text could not be read",
                exc_info=RedactedFailure.of(inner),
            )
            return ""
