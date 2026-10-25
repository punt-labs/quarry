"""How an operating-system fault is named in a log line."""

from __future__ import annotations

import errno
from typing import final


@final
class OsFault:
    """Name a fault by its errno (``EMFILE``), which never quotes a path."""

    __slots__ = ()

    @staticmethod
    def named(exc: BaseException) -> str:
        """Return *exc*'s errno name, or its class name when it has none."""
        code = exc.errno if isinstance(exc, OSError) else None
        return errno.errorcode.get(code or 0, type(exc).__name__)
