"""Mask the URL secrets and control characters in a finished log line."""

from __future__ import annotations

import re
from typing import ClassVar, final

from quarry.authority_mask import AuthorityMask
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

    The contract, in the order it is applied:

    1. The record is cut at :attr:`MAX_RECORD` bytes of text, with a visible marker.
    2. Control characters are escaped (``\\x1b``), so every later step reads the
       text that is written.  Newlines are kept; :meth:`one_line` escapes them.
    3. The userinfo of every ``scheme://`` authority is masked as
       :class:`~quarry.authority_mask.AuthorityMask` specifies: to the last ``@``,
       never across a line break except for the one continuation it names, and
       with everything outside the masked span kept exactly.
    4. Each whitespace-free token is then worked alone.  A token longer than
       :attr:`MAX_TOKEN` is replaced; a URL becomes its log form; a bare
       ``user:password@`` and a ``?query`` are masked.

    Steps 2 to 4 repeat until a pass changes nothing (the mask one pass leaves can
    read differently to the next), so redacting a record twice gives the record
    redacted once.  After :attr:`PASSES` passes without a fixed point the record is
    withheld: :meth:`settle` says so with ``None``, for a caller that has the
    record and can say what was withheld.  Redaction never logs.

    A line with no control character, no ``scheme://``, no
    ``user:password@``, no ``?query`` and no token over :attr:`MAX_TOKEN` comes back
    as it went in; both claims are checked against generated text in
    ``tests/test_log_redactor_oracle.py``.

    It runs under the handler lock and holds the GIL, so its cost must be linear
    in the record: every pattern is bounded, a token is worked once, and a
    pattern runs only on a token that holds the character it needs.
    """

    __slots__ = ()

    MAX_TOKEN: ClassVar[int] = 2048
    MAX_RECORD: ClassVar[int] = 64 * 1024
    PASSES: ClassVar[int] = 8

    # Each closing bracket and the opener it pairs with.
    _OPENERS: ClassVar[dict[str, str]] = {")": "(", "]": "["}

    _TOKEN: ClassVar[re.Pattern[str]] = re.compile(r"\S+")
    # The authority's userinfo runs to its last "@" before the first "/", "?" or "#"
    # whatever it holds (a quote or an angle bracket included), so a password never
    # ends a match early.  The host is the rest of the authority, and is shown only
    # if it is a plain host name or bracketed IP literal with a numeric port.
    _SCHEME_URL: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<![A-Za-z0-9+.\-])(?P<scheme>[A-Za-z0-9+.\-]{1,2047})://"
        r"(?:[^\s/\\?#]*@)?(?P<host>[^\s/\\?#'\"<>@]*)"
        r"(?P<path>[^\s?#'\"<>]*)(?:[?#]\S*)?"
    )
    _FORM: ClassVar[re.Pattern[str]] = re.compile(r"/…\(segments=\d+\)[).,;:\]}]*")
    # Scheme-less "user:password@": tried only where a run of user characters
    # begins, so each run is scanned once however long the token is.
    _USERINFO: ClassVar[re.Pattern[str]] = re.compile(
        r"(?<![^\s/@:'\"<>])[^\s/@:'\"<>]{0,2048}:[^\s/@'\"<>]{0,2048}@"
    )
    _QUERY: ClassVar[re.Pattern[str]] = re.compile(r"\?\S{1,2048}")
    _FRAGMENT: ClassVar[re.Pattern[str]] = re.compile(r"#\S{1,2048}")
    _CONTROL: ClassVar[re.Pattern[str]] = re.compile(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]"
    )
    # Line breaks and the bidi marks that reorder or hide what follows.
    _MARKS = r"\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069"
    _LINE_BREAKS: ClassVar[re.Pattern[str]] = re.compile(f"[\r\n{_MARKS}]")
    _BIDI: ClassVar[re.Pattern[str]] = re.compile(f"[\r{_MARKS}]")

    @classmethod
    def settle(cls, text: str) -> str | None:
        """Return *text* with URL userinfo, query and fragment masked, or ``None``.

        ``None`` means the passes did not settle, so nothing of *text* may be shown.

        It logs nothing: a log call from inside a handler's ``format`` would
        reach that same handler.
        """
        text, marker = cls._bounded(text)
        # A mask can leave text the next pass reads differently (a placeholder
        # beside a ``:`` or a ``/``), so the passes repeat until one changes
        # nothing.  That is what makes redacting a second time a no-op.
        for _ in range(cls.PASSES):
            masked = cls._once(text)
            if masked == text:
                return text + marker
            text = masked
        # No fixed point: what is left may still hold what a pass would mask.
        return None

    @classmethod
    def _bounded(cls, text: str) -> tuple[str, str]:
        """Return *text* cut to :attr:`MAX_RECORD`, and the marker saying so."""
        if len(text) <= cls.MAX_RECORD:
            return text, ""
        dropped = len(text[cls.MAX_RECORD :].encode("utf-8", errors="replace"))
        return text[: cls.MAX_RECORD], f" […truncated {dropped} bytes]"

    @classmethod
    def _once(cls, text: str) -> str:
        """Return *text* through one pass: escapes, authorities, then each token."""
        # Escaped first, so the text every later step sees is the text that is
        # written.
        text = cls._CONTROL.sub(cls._escape, text)
        if ":" in text and "@" in text:
            text = AuthorityMask.mask(text)
        return cls._TOKEN.sub(cls._token, text)

    @classmethod
    def one_line(cls, message: str) -> str:
        """Return *message* with line breaks and bidi controls escaped.

        Applied to a record's message alone, so a name or a URL carrying a newline
        cannot forge a log line, while a traceback keeps its own.
        """
        return cls._LINE_BREAKS.sub(cls._escape, message)

    @classmethod
    def quoted(cls, value: str) -> str:
        """Return *value* as one quoted line: ``repr`` with line breaks escaped.

        For text that is shown to a reader, human or model, but that an outside
        party chose (a directory name, say): quoting marks where it begins and
        ends, and no character in it can start a new line or hide the rest of
        this one.
        """
        return cls.one_line(repr(value))

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
            # A character the path stops at (a quote, say) leaves a later
            # fragment outside the URL match, and a fragment can carry a token.
            token = cls._FRAGMENT.sub("#[fragment]", token)
        elif cls._is_host_query(token):
            # ``host:port?...``: the query starts here, so an ``@`` inside it is no
            # userinfo boundary and none of it may show.  If one follows, the text
            # may instead be ``user:digits?...@host``, so the head is masked too.
            head, _, query = token.partition("?")
            return ("[userinfo]" if "@" in query else head) + "?[query]"
        # A password may hold "?" and "#", so userinfo goes before the query.
        if "@" in token:
            token = cls._USERINFO.sub("[userinfo]@", token)
        return cls._QUERY.sub("?[query]", token) if "?" in token else token

    @staticmethod
    def _is_host_query(token: str) -> bool:
        """Return whether *token* is a bare host (and port) followed by a query."""
        head, _, query = token.partition("?")
        return bool(query) and bool(AuthorityMask.HOST.fullmatch(head))

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

        Delimiters that end the match but are no part of a URL's meaning stay
        outside it and follow the log form unchanged.  A URL already in that form
        is returned as it stands, so redacting twice changes nothing.
        """
        core, tail = cls._split_tail(match.group(), match.string[: match.start()])
        inner = cls._SCHEME_URL.fullmatch(core)
        return cls._log_form(inner or match) + (tail if inner else "")

    @classmethod
    def _split_tail(cls, url: str, outside: str) -> tuple[str, str]:
        """Return *url* without its trailing delimiters, and what is kept of them.

        *outside* is the text of the token before the URL.  Prose after a query or
        a fragment is not kept as it stands, since it may be the end of a secret:
        only as many closers survive as *outside* has unmatched openers.  A ``:``
        after a bare host stays in the URL, where the host position is masked.
        """
        core, tail = cls._peel(url)
        rest = core.partition("://")[2]
        if ":" in tail and not any(char in rest for char in "/?#"):
            return url, ""
        if "?" in core or "#" in core:
            return core, cls._closers(tail, outside)
        return core, tail

    @classmethod
    def _closers(cls, tail: str, outside: str) -> str:
        """Return the closers of *tail* that close an opener left open in *outside*."""
        open_count = {
            closer: outside.count(opener) - outside.count(closer)
            for closer, opener in cls._OPENERS.items()
        }
        kept = []
        for char in tail:
            if open_count.get(char, 0) > 0:
                open_count[char] -= 1
                kept.append(char)
        return "".join(kept)

    @classmethod
    def _peel(cls, url: str) -> tuple[str, str]:
        """Split off the usual linkify tail: ``, . : ;`` and any unmatched closer.

        A ``)`` or ``]`` is prose when it closes nothing opened inside the URL.
        """
        end = len(url)
        while end and cls._is_prose(url, end):
            end -= 1
        return url[:end], url[end:]

    @classmethod
    def _is_prose(cls, url: str, end: int) -> bool:
        """Return whether the character before *end* is prose after the URL."""
        last = url[end - 1]
        opener = cls._OPENERS.get(last)
        if opener is None:
            return last in ".,:;"
        return url.count(last, 0, end) > url.count(opener, 0, end)

    @classmethod
    def _log_form(cls, match: re.Match[str]) -> str:
        """Return the log form of a URL match."""
        host = match.group("host")
        if not AuthorityMask.HOST.fullmatch(host):
            host = "[host]"
        path = match.group("path")
        if cls._FORM.fullmatch(path):
            return f"{match.group('scheme')}://{host}{path}"
        return LogForm.of(match.group("scheme"), host, path)
