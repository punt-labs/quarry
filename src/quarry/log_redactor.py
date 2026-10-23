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
    ``scheme://host/…(segments=1)``, a bare ``user:pw@`` or ``?query`` is masked, and a
    control character is escaped so it cannot forge or hide a log line.

    It runs under the handler lock and holds the GIL, so its cost must be linear
    in the line: the whole record is cut at :attr:`MAX_RECORD` (with a visible
    marker), a whitespace-free token longer than :attr:`MAX_TOKEN` is replaced
    before any pattern sees it, and a pattern runs only on a token that holds the
    character it needs.
    """

    __slots__ = ()

    MAX_TOKEN: ClassVar[int] = 2048
    MAX_RECORD: ClassVar[int] = 64 * 1024

    _TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"\S+")
    # The authority's userinfo runs to its last "@" before the first "/", "?" or "#"
    # whatever it holds (a quote or an angle bracket included), so a password never
    # ends a match early.  The host is the rest of the authority, and is shown only
    # if it is a plain host name or bracketed IP literal with a numeric port.
    _SCHEME_URL: ClassVar[re.Pattern[str]] = re.compile(
        r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]{0,31})://"
        r"(?:[^\s/\\?#]*@)?(?P<host>[^\s/\\?#'\"<>@]*)"
        r"(?P<path>[^\s?#'\"<>]*)(?:[?#][^\s'\"<>]*)?"
    )
    _HOST: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.%a-z]+\])(?::\d{1,5})?"
    )
    # urlsplit deletes tab, CR and LF anywhere in a URL, so a URL may be broken by
    # them in its scheme, its "://", its authority or its path.  One bounded,
    # single-class span finds such a candidate and the breaks are deleted from it.
    _BREAKS: ClassVar[re.Pattern[str]] = re.compile(r"[\t\r\n]+")
    _BROKEN_URL: ClassVar[re.Pattern[str]] = re.compile(
        r"[A-Za-z](?:[\t\r\n]*[A-Za-z0-9+.\-]){0,31}[\t\r\n]*:[\t\r\n]*/[\t\r\n]*/"
        r"[\S\t\r\n]{0,2048}"
    )
    # urlsplit strips whitespace inside a URL, so "user:pw<space>@host" is one URL.
    _SPLIT_AUTHORITY: ClassVar[re.Pattern[str]] = re.compile(
        r"(?P<head>[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s/\\?#]*)\s+(?=@)"
    )
    _FORM: ClassVar[re.Pattern[str]] = re.compile(r"/…\(segments=\d+\)[).,;:\]}]*")
    _USERINFO: ClassVar[re.Pattern[str]] = re.compile(
        r"[^\s/@:'\"<>]{1,128}:[^\s/@'\"<>]{0,128}@"
    )
    _QUERY: ClassVar[re.Pattern[str]] = re.compile(r"\?[^\s'\"<>#]{1,2048}")
    _CONTROL: ClassVar[re.Pattern[str]] = re.compile(
        "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]"
    )
    # Line breaks and the bidi marks that reorder or hide what follows.
    _MARKS = "\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069"
    _LINE_BREAKS: ClassVar[re.Pattern[str]] = re.compile(f"[\r\n{_MARKS}]")
    _BIDI: ClassVar[re.Pattern[str]] = re.compile(f"[\r{_MARKS}]")

    @classmethod
    def redact(cls, text: str) -> str:
        """Return *text* with URL userinfo, query and fragment masked."""
        if len(text) > cls.MAX_RECORD:
            dropped = len(text[cls.MAX_RECORD :].encode("utf-8", errors="replace"))
            text = f"{text[: cls.MAX_RECORD]}[…truncated {dropped} bytes]"
        if ":" in text and cls._BREAKS.search(text):
            text = cls._BROKEN_URL.sub(cls._joined, text)
        if "://" in text and "@" in text:
            text = cls._SPLIT_AUTHORITY.sub(r"\g<head>", text)
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
    def _joined(cls, match: re.Match[str]) -> str:
        """Return a URL candidate without the tabs, CRs and LFs urlsplit deletes."""
        return cls._BREAKS.sub("", match.group())

    @classmethod
    def continuation(cls, block: str) -> str:
        """Return a multi-line *block* with every line indented by a tab.

        For a traceback or a stack, whose own newlines are kept but must never
        let a line begin a record: an exception message can hold anything,
        including a forged timestamp.  Carriage returns and bidi marks are
        escaped.
        """
        escaped = cls._BIDI.sub(cls._escape, block.rstrip("\n"))
        return "\t" + escaped.replace("\n", "\n\t")

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
        if "://" in token:
            token = cls._SCHEME_URL.sub(cls._bare_url, token)
        if "@" in token:
            token = cls._USERINFO.sub("[userinfo]@", token)
        return cls._QUERY.sub("?[query]", token) if "?" in token else token

    @staticmethod
    def _escape(match: re.Match[str]) -> str:
        char = match.group()
        code = ord(char)
        if char in "\r\n":
            return repr(char)[1:-1]
        if code <= 0xFF:
            return f"\\x{code:02x}"
        return f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}"

    @classmethod
    def _bare_url(cls, match: re.Match[str]) -> str:
        """Return a URL match as ``scheme://host[:port]/…(segments=N)``.

        A URL already in that form is returned as it stands, so redacting twice
        changes nothing.
        """
        host = match.group("host")
        if not cls._HOST.fullmatch(host):
            host = "[host]"
        path = match.group("path")
        if cls._FORM.fullmatch(path):
            return f"{match.group('scheme')}://{host}{path}"
        return LogForm.of(match.group("scheme"), host, path)
