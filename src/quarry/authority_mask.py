"""Mask the userinfo of every ``scheme://`` authority in a finished log record."""

from __future__ import annotations

import re
from typing import ClassVar, final


@final
class AuthorityMask:
    """Replace ``scheme://userinfo@`` with ``scheme://[userinfo]@``, line by line.

    The contract, which ``tests/test_log_redactor_oracle.py`` checks against a
    generated grammar:

    1. **Opener.**  A ``scheme:`` followed by two or more slashes opens an
       authority.  Whitespace may sit between the scheme and the colon, and
       between any two of those characters; a scheme of ``http``, ``https``,
       ``ftp``, ``ftps``, ``ws`` or ``wss`` may also be broken by whitespace
       between its letters.  Any other scheme is read unbroken, because joining
       arbitrary letters would eat the last word of the previous line.
    2. **Authority.**  The text after the slashes up to the next ``/``, ``\\``,
       ``?`` or ``#``, and never across a line break (``\\n`` or ``\\r``).
    3. **Mask.**  When the authority holds an ``@``, everything from the scheme
       up to the last ``@`` of the authority is replaced.  The host, path and
       the rest of the line are not touched here.
    4. **Joining within a line.**  Whitespace is part of the userinfo only when
       the text since the slashes has *userinfo shape* (it is not a plain host,
       optionally with a numeric port) and holds no ``@`` yet.  A host-only URL
       followed by a space and text containing an ``@`` is therefore not joined.
       The authority ends at the first ``@`` of a whitespace-free run, so a
       password holding both ``@`` and whitespace after the ``@`` is masked only
       to the last ``@`` of that run.
    5. **Across a line break.**  A line that ends inside an authority of
       userinfo shape (no ``/``, ``?``, ``#`` or ``@`` yet) is masked to its last
       authority character, and marks the next line's first whitespace-free
       token as its continuation: that token alone is masked, up to its last
       ``@`` before a ``/``, ``?`` or ``#``.
    6. **Preservation.**  Every character outside a masked span is kept exactly.
       No line is deleted or merged, and no host is re-attributed.

    Every scan moves forward over disjoint text and stops at the first ``/`` the
    next opener must contain, so the cost is linear in the record.
    """

    __slots__ = ()

    _EOL: ClassVar[int] = -1
    _STOPPED: ClassVar[int] = -2

    # A plain host name or bracketed IP literal, optionally with a numeric port:
    # what an authority holds when it has no userinfo.
    HOST: ClassVar[re.Pattern[str]] = re.compile(
        r"(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.%a-z]+\])(?::\d{1,5})?"
    )
    # LogRedactor escapes the control characters before this runs.  The ones that
    # are whitespace (tab apart) reach it as text, ``\x0b`` and the like, and
    # are still whitespace here: a record then reads the same however many times
    # it is redacted.
    _WS_ESCAPES: ClassVar[tuple[str, ...]] = (
        "\\x0b",
        "\\x0c",
        "\\x1c",
        "\\x1d",
        "\\x1e",
        "\\x1f",
        "\\x85",
        "\\u2028",
        "\\u2029",
    )
    _WS_ESC = r"\\(?:x(?:0b|0c|1[c-f]|85)|u202[89])"
    _BLANK = rf"(?:[^\S\r\n]|{_WS_ESC})"
    _SP = rf"{_BLANK}*"
    _BOUNDARY = (
        r"(?:(?<![A-Za-z0-9+.\-])"
        + "".join(f"|(?<={re.escape(esc)})" for esc in _WS_ESCAPES)
        + ")"
    )
    _LINE: ClassVar[re.Pattern[str]] = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
    _OPENER: ClassVar[re.Pattern[str]] = re.compile(
        rf"{_BOUNDARY}(?P<scheme>"
        rf"(?i:h{_SP}t{_SP}t{_SP}p(?:{_SP}s)?|f{_SP}t{_SP}p(?:{_SP}s)?|w{_SP}s(?:{_SP}s)?)"
        r"|[A-Za-z][A-Za-z0-9+.\-]{0,31})"
        rf"{_SP}:{_SP}(?:/{_SP}){{2,}}"
    )
    _ATOM: ClassVar[re.Pattern[str]] = re.compile(
        rf"(?:(?!{_WS_ESC})\\(?:x[0-9a-f]{{2}}|u[0-9a-f]{{4}})|[^\s/\\?#])*"
    )
    _GAP: ClassVar[re.Pattern[str]] = re.compile(rf"{_BLANK}+")
    _WHITESPACE: ClassVar[re.Pattern[str]] = re.compile(rf"(?:\s|{_WS_ESC})+")
    # "/", "?" and "#" end an authority, and so does a backslash that does not
    # begin an escape sequence.
    _STOP: ClassVar[re.Pattern[str]] = re.compile(
        r"[/?#]|\\(?!x[0-9a-f]{2}|u[0-9a-f]{4})"
    )

    @classmethod
    def mask(cls, text: str) -> str:
        """Return *text* with the userinfo of every URL authority masked."""
        out: list[str] = []
        open_as, span = "", (0, 0)
        for line in cls._LINE.finditer(text):
            cont = cls._continuation(line.group(), open_as) if open_as else (0, 0)
            if cont[1] and open_as == "host":
                # The line before was only a candidate; now it is known to hold
                # the start of the userinfo.
                out[-1] = out[-1][: span[0]] + "[userinfo]" + out[-1][span[1] :]
            masked, open_as, span = cls._mask_line(line.group(), cont)
            out.append(masked)
        return "".join(out)

    @classmethod
    def _mask_line(
        cls, line: str, cont: tuple[int, int]
    ) -> tuple[str, str, tuple[int, int]]:
        """Return *line* masked, how it ends, and the span of an open host.

        It ends ``""`` (closed), ``"userinfo"`` (inside an authority that already
        has userinfo shape, masked to its end) or ``"host"`` (inside one that has
        not, left as it is, with its span in the returned text).
        """
        parts: list[str] = []
        kept = search = 0
        if cont[1]:
            parts.append(line[: cont[0]] + "[userinfo]@")
            kept = search = cont[1]
        ends, span = "", (0, 0)
        while opener := cls._OPENER.search(line, search):
            end, ends = cls._scan(line, opener.end())
            span = (opener.end(), end)
            if end and ends != "host":
                scheme = cls._WHITESPACE.sub("", opener.group("scheme"))
                at = "" if ends else "@"
                parts.append(line[kept : opener.start()] + f"{scheme}://[userinfo]{at}")
                kept = search = end
            else:
                search = opener.end()
        parts.append(line[kept:])
        masked = "".join(parts)
        shift = len(masked) - len(line)
        return masked, ends, (span[0] + shift, span[1] + shift)

    @classmethod
    def _continuation(cls, line: str, open_as: str) -> tuple[int, int]:
        """Return the span of the userinfo that opens *line*, or ``(0, 0)``.

        It is the line's first whitespace-free token, up to its last ``@``, and
        only if what precedes that ``@`` is userinfo (or the line before it
        already was), so an email address after a host-only URL is left alone.
        """
        lead = cls._GAP.match(line)
        start = lead.end() if lead else 0
        text = cls._atom(line, start).group()
        if "@" not in text:
            return 0, 0
        at = text.rindex("@")
        if open_as == "host" and cls._is_email(text, at, line, start):
            return 0, 0
        return start, start + at + 1

    @classmethod
    def _is_email(cls, text: str, at: int, line: str, pos: int) -> bool:
        """Return whether ``text[:at] @ …`` reads as an address, not as userinfo.

        It does when what precedes the ``@`` is a plain host name and no
        ``/``, ``?`` or ``#`` follows straight after the token: a URL's host is
        followed by its path, an address in prose is not.
        """
        ends_url = cls._STOP.match(line, pos + len(text)) is not None
        return bool(cls.HOST.fullmatch(text[:at])) and not ends_url

    @classmethod
    def _atom(cls, line: str, pos: int) -> re.Match[str]:
        """Return the run of authority characters at *pos* (possibly empty)."""
        # The pattern can match the empty string, so finditer always yields at *pos*.
        return next(cls._ATOM.finditer(line, pos))

    @classmethod
    def _scan(cls, line: str, start: int) -> tuple[int, str]:
        """Return where the masked span ends (0 if none), and how the line ends.

        *start* is just after the slashes.  The span ends after the last ``@``.
        If the line ends inside the authority first, the second value is
        ``"userinfo"`` when the authority already has userinfo shape (and the
        span then ends after its last character) and ``"host"`` when it does not.
        A part after an ``@`` that is not a host name is still userinfo, so the
        scan goes on to a later ``@`` in the same run of text.
        """
        pos, best = start, 0
        shaped, seen = False, False
        while True:
            atom = cls._atom(line, pos)
            text = atom.group()
            if "@" in text:
                found, ends = cls._after_at(text, line, pos, shaped=shaped)
                if not found:
                    return best, ""
                best, shaped = found, True
                if ends:
                    return best, ""
            else:
                seen = seen or bool(text)
                shaped = shaped or cls._is_userinfo(text)
            nxt = cls._next(line, atom.end())
            if nxt == cls._EOL:
                # An authority with nothing in it yet has no host to be only a host.
                return atom.end(), ("userinfo" if shaped or not seen else "host")
            if nxt == cls._STOPPED:
                return best, ""
            pos = nxt

    @classmethod
    def _after_at(
        cls, text: str, line: str, pos: int, *, shaped: bool
    ) -> tuple[int, bool]:
        """Return the offset after the last ``@`` of *text* (0 if it is an address).

        The second value is true when the authority ends there.  A part after the
        ``@`` that is not a host is still userinfo (``u@x: pw@host``).
        """
        at = text.rindex("@")
        tail = text[at + 1 :]
        ends = not tail or bool(cls.HOST.fullmatch(tail))
        if ends and not shaped and cls._is_email(text, at, line, pos):
            return 0, True
        return pos + at + 1, ends

    @classmethod
    def _is_userinfo(cls, text: str) -> bool:
        """Return whether *text* cannot be a bare host (it has a ``:`` or the like)."""
        return bool(text) and not cls.HOST.fullmatch(text)

    @classmethod
    def _next(cls, line: str, end: int) -> int:
        """Return where the authority goes on after *end*, or why it does not."""
        gap = cls._GAP.match(line, end)
        after = gap.end() if gap else end
        if after >= len(line) or line[after] in "\r\n":
            return cls._EOL
        if after == end or not cls._atom(line, after).group():
            return cls._STOPPED
        return after
