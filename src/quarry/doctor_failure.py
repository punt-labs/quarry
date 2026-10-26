"""Say, in one line, why a check could not run."""

from __future__ import annotations

import re
from typing import ClassVar, final


@final
class CheckFailure:
    """Describe the exception that stopped a check, safely, for a report line.

    A broken native stack can fail an import or a call in several ways, so a
    check that guards one of them catches :attr:`BROKEN_STACK`.  The description
    is the exception's class and the first line of its message with control
    characters removed and the length bounded, so a hostile or multi-line message
    cannot spoil the report.
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
        """Return ``Class: first line of the message`` (just the class if none)."""
        text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        text = cls._CONTROL.sub("?", text)[: cls._MAX_LINE]
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

    @classmethod
    def import_message(cls, attempted: str, exc: Exception) -> str:
        """Say which module failed to load and why.

        An ``ImportError`` names the module that really failed; any other
        failure can only name what was being imported.  Reinstalling is advised
        only when the module is simply missing.
        """
        name = getattr(exc, "name", None) if isinstance(exc, ImportError) else None
        message = f"cannot load {name or attempted} ({cls.detail(exc)})"
        if isinstance(exc, ModuleNotFoundError):
            return f"{message}; reinstall quarry"
        return message
