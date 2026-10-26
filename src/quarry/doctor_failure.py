"""Say, in one line, why a check could not run."""

from __future__ import annotations

import errno
import logging
import re
import subprocess
from typing import ClassVar, final

from pydantic import ValidationError

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
        if isinstance(exc, subprocess.TimeoutExpired):
            return f"{name} after {exc.timeout:g}s"  # never its argv
        if isinstance(exc, OSError) and exc.errno is not None:
            return f"{name}: {FailureReason.os_name(exc)}"  # its message quotes paths
        text = cls.line(cls._text(exc))
        return f"{name}: {text}" if text else name

    @staticmethod
    def is_missing(exc: OSError | ValueError) -> bool:
        """Return whether *exc* means "not there" rather than "cannot be read".

        Only ``ENOENT`` is a thing that is not there; anything else (denied, I/O,
        a bare ``OSError`` or ``ValueError``) is a fault whose cause is named.
        """
        return isinstance(exc, OSError) and exc.errno == errno.ENOENT

    @classmethod
    def settings_reason(cls, exc: Exception) -> str:
        """Name the first invalid setting and why, for a settings failure."""
        if isinstance(exc, ValidationError) and (errors := exc.errors()):
            first = errors[0]
            field = ".".join(str(part) for part in first["loc"])
            return f"{cls.line(field)}: {cls.line(first['msg'])}"
        return cls.detail(exc)

    @classmethod
    def line(cls, text: str) -> str:
        """Return the first line of *text*, printable and bounded."""
        stripped = text.strip()
        first = stripped.splitlines()[0] if stripped else ""
        return cls._CONTROL.sub("?", first)[: cls._MAX_LINE]

    @classmethod
    def because(cls, output: str | None) -> str:  # None: the process wrote nothing
        """Return ``: <last line of output>``, sanitized and bounded, or nothing."""
        lines = (output or "").strip().splitlines()
        return f": {cls.line(lines[-1])}" if lines else ""

    @classmethod
    def origin(cls, exc: BaseException) -> str | None:  # None: no frame names one
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
