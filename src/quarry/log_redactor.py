"""Mask the URL secrets and control characters in a finished log line."""

from __future__ import annotations

import re
from typing import ClassVar, final

from quarry.log_form import LogForm


@final
class LogRedactor:
    """Strip what a URL or a control character can smuggle into a log line.

    The last line of defence, applied to the finished text (message, arguments and
    traceback alike) at the one place every record is written.  The types that
    carry a URL are the guarantee; this catches whatever a library quotes
    regardless: ``scheme://user:pw@host/path?query#fragment`` becomes
    ``scheme://host/…(1 segment)``, a bare ``user:pw@`` or ``?query`` is masked, and a
    control character is escaped so it cannot forge or hide a log line.

    It runs under the handler lock and holds the GIL, so its cost must be linear
    in the line: every quantifier is bounded, and a whitespace-free token longer
    than :attr:`MAX_TOKEN` is replaced before any pattern sees it.
    """

    __slots__ = ()

    MAX_TOKEN: ClassVar[int] = 2048

    _TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"\S+")
    _SCHEME_URL: ClassVar[re.Pattern[str]] = re.compile(
        r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,31})://(?P<auth>[^\s/?#'\"<>]{0,256})"
        r"(?P<path>[^\s?#'\"<>]{0,2048})(?:[?#][^\s'\"<>]{0,2048})?"
    )
    _USERINFO: ClassVar[re.Pattern[str]] = re.compile(
        r"[^\s/@:'\"<>]{1,128}:[^\s/@'\"<>]{0,128}@"
    )
    _QUERY: ClassVar[re.Pattern[str]] = re.compile(r"\?[^\s'\"<>#]{1,2048}")
    _CONTROL: ClassVar[re.Pattern[str]] = re.compile(
        "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]"
    )
    _LINE_BREAKS: ClassVar[re.Pattern[str]] = re.compile(
        "[\r\n\u202a-\u202e\u2066-\u2069]"
    )

    @classmethod
    def redact(cls, text: str) -> str:
        """Return *text* with URL userinfo, query and fragment masked."""
        text = cls._TOKEN.sub(cls._token, text)
        return cls._CONTROL.sub(cls._escape, text)

    @classmethod
    def one_line(cls, message: str) -> str:
        """Return *message* with line breaks and bidi controls escaped.

        Applied to a record's message alone, so a name or a URL carrying a newline
        cannot forge a log line, while a traceback keeps its own.
        """
        return cls._LINE_BREAKS.sub(cls._escape, message)

    @classmethod
    def _token(cls, match: re.Match[str]) -> str:
        """Return one whitespace-free token, masked or replaced when too long.

        Every pattern excludes whitespace, so working a token at a time finds what
        a pass over the whole line would, and the length test is one comparison
        per token rather than a quantifier retried at every offset.
        """
        token = match.group()
        if len(token) > cls.MAX_TOKEN:
            return "[…]"
        token = cls._SCHEME_URL.sub(cls._bare_url, token)
        token = cls._USERINFO.sub("[userinfo]@", token)
        return cls._QUERY.sub("?[query]", token)

    @staticmethod
    def _escape(match: re.Match[str]) -> str:
        char = match.group()
        code = ord(char)
        if char in "\r\n":
            return repr(char)[1:-1]
        if code <= 0xFF:
            return f"\\x{code:02x}"
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"

    @staticmethod
    def _bare_url(match: re.Match[str]) -> str:
        """Return a URL match as ``scheme://host[:port]/…(N segments)``."""
        host = match.group("auth").rpartition("@")[2]
        return LogForm.of(match.group("scheme"), host, match.group("path"))
