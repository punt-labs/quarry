"""The syntactic rules a URL must meet before any parser or socket sees it."""

from __future__ import annotations

from typing import final
from urllib.parse import SplitResult, urlsplit

_MALFORMED = "malformed URL"
_MAX_LENGTH = 8192


@final
class UrlSyntax:
    """One canonical reading of a URL, shared by every gate and parser.

    Disagreement between parsers is how a secret escapes: ``urlsplit`` accepts a
    space or a control character that ``http.client`` later quotes in an
    exception, a ``@`` in the netloc hides userinfo from one parser and not
    another, and a bad port parses lazily and fails far from the gate.  So the URL
    is admitted only if it is plain: no userinfo, no ``\\`` or ``%`` in the
    netloc, no control, space or other whitespace character anywhere, and a port
    that is absent or in ``1..65535``, and a length of at most 8192 characters.
    After that, ``urlsplit``, ``urllib``, ``http.client`` and the sitemap parser
    all see the same ``[host][:port]``.

    A refusal carries the fixed text ``malformed URL`` and no chained cause:
    Python's own messages quote the netloc, userinfo included.
    """

    __slots__ = ()

    @classmethod
    def parse(cls, raw: str) -> SplitResult:
        """Return *raw* split into parts, or raise ``ValueError("malformed URL")``."""
        if len(raw) > _MAX_LENGTH or any(cls._is_unprintable(ch) for ch in raw):
            raise ValueError(_MALFORMED)
        parts = cls._split(raw)
        if not cls._is_plain(parts):
            raise ValueError(_MALFORMED)
        return parts

    @staticmethod
    def _is_unprintable(ch: str) -> bool:
        """Return whether *ch* is a control, space or other whitespace character."""
        return ch <= " " or "\x7f" <= ch <= "\x9f" or ch.isspace()

    @staticmethod
    def _split(raw: str) -> SplitResult:
        """Split *raw* and force the lazy port parse, hiding Python's own message."""
        try:
            parts = urlsplit(raw)
            _ = parts.port
        except ValueError:
            raise ValueError(_MALFORMED) from None
        return parts

    @staticmethod
    def _is_plain(parts: SplitResult) -> bool:
        """Return whether the netloc is plain: no userinfo, escape or backslash."""
        netloc = parts.netloc
        return not ("@" in netloc or "\\" in netloc or "%" in netloc) and (
            parts.port != 0
        )
