"""The ``<!-- media_type: X -->`` line that labels a captured non-HTML body."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, final


@final
@dataclass(frozen=True, slots=True)
class CaptureMarker:
    """The one-line HTML comment naming a stored body's media type.

    A reader, and a downstream grep, learn the shape of a non-HTML capture from
    it, and the binary-content check skips it when looking for a signature.
    ``media_type`` is the ``Content-Type`` primary token of an HTTP response,
    which the server controls: interpolating it verbatim would let a header
    carrying whitespace, control bytes or the ``-->`` needle break the
    single-line contract or escape the comment.  Construction therefore strips
    it to the RFC 6838-like shape (alphanumerics plus ``/.+;=_-``), caps it at
    128 characters, and substitutes ``application/octet-stream`` when nothing
    survives.
    """

    media_type: str

    _UNSAFE: ClassVar[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9/.+;=_-]")
    _LEADING: ClassVar[re.Pattern[str]] = re.compile(
        r"\A<!-- media_type: [^\n]*? -->\n?"
    )
    _MAX_LEN: ClassVar[int] = 128
    _FALLBACK: ClassVar[str] = "application/octet-stream"

    def __post_init__(self) -> None:
        cleaned = self._UNSAFE.sub("", self.media_type)[: self._MAX_LEN]
        object.__setattr__(self, "media_type", cleaned or self._FALLBACK)

    def render(self) -> str:
        """Return the marker line, newline included."""
        return f"<!-- media_type: {self.media_type} -->\n"

    @classmethod
    def without(cls, text: str) -> str:
        """Return *text* with a leading marker line removed."""
        return cls._LEADING.sub("", text, count=1)
