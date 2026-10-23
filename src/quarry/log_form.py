"""The one shape a URL takes in a log: host and port, never any of the path."""

from __future__ import annotations

import unicodedata
from typing import final

_UNSAFE = frozenset({"Cc", "Cf", "Zl", "Zp"})


@final
class LogForm:
    """Render ``scheme://host[:port]/…(segments=N)``.

    A secret can live in any path segment and no heuristic recognizes every token
    (a JWT, padded base64, a short code), so a log shows how many segments a path
    had and nothing of what they said.
    """

    __slots__ = ()

    @staticmethod
    def of(scheme: str, netloc: str, path: str) -> str:
        """Return the log form of a URL given its scheme, netloc and path."""
        count = len([part for part in path.split("/") if part])
        return f"{LogForm._safe(scheme)}://{LogForm._safe(netloc)}/…(segments={count})"

    @staticmethod
    def _safe(text: str) -> str:
        """Escape the control, format and separator characters in *text*.

        ``UrlSyntax`` refuses them, so only the tolerant reading of a refused URL
        can carry one here; they must not reorder or forge a log line.
        """
        return "".join(LogForm._escaped(ch) for ch in text)

    @staticmethod
    def _escaped(ch: str) -> str:
        """Return *ch*, or its ``\\xNN`` / ``\\uNNNN`` escape when it is unsafe."""
        if unicodedata.category(ch) not in _UNSAFE:
            return ch
        return f"\\x{ord(ch):02x}" if ord(ch) <= 0xFF else f"\\u{ord(ch):04x}"
