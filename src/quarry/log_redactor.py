"""Mask the URL secrets and control characters in a finished log line."""

from __future__ import annotations

import re
from typing import ClassVar, final


@final
class LogRedactor:
    """Strip what a URL or a control character can smuggle into a log line.

    The last line of defence, applied to the finished text (message, arguments and
    traceback alike) at the one place every record is written.  The types that
    carry a URL are the guarantee; this catches whatever a library quotes
    regardless: ``scheme://user:pw@host/path?query#fragment`` becomes
    ``scheme://host/path``, a bare ``user:pw@`` or ``?query`` is masked, and a
    control character is escaped so it cannot forge or hide a log line.
    """

    __slots__ = ()

    _SCHEME_URL: ClassVar[re.Pattern[str]] = re.compile(
        r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<auth>[^\s/?#'\"<>]*)"
        r"(?P<path>[^\s?#'\"<>]*)(?:[?#][^\s'\"<>]*)?"
    )
    _USERINFO: ClassVar[re.Pattern[str]] = re.compile(r"[^\s/@'\"<>]+:[^\s/@'\"<>]*@")
    _QUERY: ClassVar[re.Pattern[str]] = re.compile(r"\?[^\s'\"<>#]+")
    _CONTROL: ClassVar[re.Pattern[str]] = re.compile(
        "[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]"
    )

    @classmethod
    def redact(cls, text: str) -> str:
        """Return *text* with URL userinfo, query and fragment masked."""
        text = cls._SCHEME_URL.sub(cls._bare_url, text)
        text = cls._USERINFO.sub("[userinfo]@", text)
        text = cls._QUERY.sub("?[query]", text)
        return cls._CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)

    @staticmethod
    def _bare_url(match: re.Match[str]) -> str:
        """Return a ``scheme://host/path`` URL match without its userinfo."""
        host = match.group("auth").rpartition("@")[2]
        return f"{match.group('scheme')}://{host}{match.group('path')}"
