"""Unit-file and env-file text, read the way systemd reads it."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, final

if TYPE_CHECKING:
    from collections.abc import Iterator


# Whitespace as systemd skips it: a space, tab, newline or carriage return.  A
# form feed or a no-break space is not whitespace to systemd.
_ASCII_SPACE = " \t\n\r"


@final
class SystemdText:
    """Read settings out of a unit or env file's text as systemd does.

    Lines are split on a newline only, a line ending in a backslash is joined
    with the next, and ``#`` and ``;`` comment lines are skipped.  Every read is
    one pass with no backtracking, so a hostile file costs time linear in its
    size.
    """

    # ASCII digits only: a Unicode digit is not a number to systemd.
    _NUMBER: ClassVar[str] = r"(?<![0-9])[0-9]{1,9}(?:\.[0-9]{1,9})?(?![0-9])"
    _TERM: ClassVar[re.Pattern[str]] = re.compile(
        rf"({_NUMBER})[ \t\n\r]*([A-Za-z]*)", re.ASCII
    )
    # The keys that set a unit's stop timeout, the last assignment of either wins.
    _STOP_KEYS: ClassVar[frozenset[str]] = frozenset({"TimeoutStopSec", "TimeoutSec"})
    # systemd's time-span units, in seconds; a bare number is seconds.  Units are
    # case-sensitive, as in systemd: ``m`` is a minute and ``M`` is a month.
    _UNITS: ClassVar[dict[str, float]] = {
        "": 1.0,
        "us": 1e-6,
        "usec": 1e-6,
        "ms": 1e-3,
        "msec": 1e-3,
        "s": 1.0,
        "sec": 1.0,
        "second": 1.0,
        "seconds": 1.0,
        "m": 60.0,
        "min": 60.0,
        "minute": 60.0,
        "minutes": 60.0,
        "h": 3600.0,
        "hr": 3600.0,
        "hour": 3600.0,
        "hours": 3600.0,
        "d": 86400.0,
        "day": 86400.0,
        "days": 86400.0,
        "w": 604800.0,
        "week": 604800.0,
        "weeks": 604800.0,
        "M": 2629800.0,  # 30.44 days, as systemd defines a month
        "month": 2629800.0,
        "months": 2629800.0,
        "y": 31557600.0,  # 365.25 days, as systemd defines a year
        "year": 31557600.0,
        "years": 31557600.0,
    }

    __slots__ = ()

    @classmethod
    def stop_timeout(cls, text: str) -> str:
        """Return the last stop-timeout value in the ``[Service]`` section, or ``""``.

        The last ``TimeoutStopSec=`` or ``TimeoutSec=`` assignment wins, and an
        empty one resets to the default (``""``).
        """
        assigned = ""
        section = ""
        for line in cls._logical_lines(text):
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            key, separator, value = line.partition("=")
            if (
                section == "Service"
                and separator
                and key.strip(_ASCII_SPACE) in cls._STOP_KEYS
            ):
                assigned = value.strip(_ASCII_SPACE)
        return assigned

    @classmethod
    def assignment(cls, text: str, name: str) -> str | None:
        """Return the last value *name* is given in an env file, else ``None``.

        For env-file text only: an env file's value may be quoted, and the quotes
        are removed.  A unit directive is never unquoted (see :meth:`stop_timeout`).
        ``None`` is the documented answer for a name the text never sets.
        """
        found: str | None = None
        for line in cls._logical_lines(text):
            key, separator, value = line.partition("=")
            if separator and key.strip(_ASCII_SPACE) == name:
                found = cls._unquoted(value.strip(_ASCII_SPACE))
        return found

    @staticmethod
    def _logical_lines(text: str) -> Iterator[str]:
        """Yield the logical lines of a unit or env file, as systemd reads them.

        Split on ``\\n`` only (a carriage return or a form feed is not a line
        break), a line ending in a backslash joined with the next, blank lines and
        ``#`` or ``;`` comments skipped.  One pass with no backtracking.
        """
        parts: list[str] = []
        for physical in text.split("\n"):
            stripped = physical.strip(_ASCII_SPACE)
            if stripped.endswith("\\"):
                parts.append(stripped[:-1])
                continue
            line = " ".join([*parts, stripped]) if parts else stripped
            parts.clear()
            if line and line[0] not in "#;":
                yield line
        if parts:
            line = " ".join(parts).strip(_ASCII_SPACE)
            if line:
                yield line

    @classmethod
    def timespan(cls, text: str) -> float:
        """Return a systemd time span in seconds.

        Terms are read left to right, each a number and an optional unit, with
        nothing skipped but whitespace.

        Raises:
            ValueError: *text* is empty, has a term that is not a number and a
                known unit, or has anything after the last term.
        """
        position = cls._skip_space(text, 0)
        total, terms = 0.0, 0
        while position < len(text):
            seconds, end = cls._term(text, position)
            total, terms = total + seconds, terms + 1
            position = cls._skip_space(text, end)
        if terms == 0:
            msg = "not a systemd time span: empty"
            raise ValueError(msg)
        return total

    @staticmethod
    def _unquoted(value: str) -> str:
        """Return *value* without one pair of matching single or double quotes."""
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            return value[1:-1]
        return value

    @staticmethod
    def _skip_space(text: str, position: int) -> int:
        """Return the index of the first non-space character at or after *position*."""
        while position < len(text) and text[position] in _ASCII_SPACE:
            position += 1
        return position

    @classmethod
    def _term(cls, text: str, position: int) -> tuple[float, int]:
        """Return the seconds of the term at *position*, and where it ends.

        Raises:
            ValueError: what is there is not a number and a known unit.
        """
        term = cls._TERM.match(text, position)
        factor = cls._UNITS.get(term.group(2)) if term else None
        if term is None or factor is None:
            msg = "not a systemd time span"
            raise ValueError(msg)
        return float(term.group(1)) * factor, term.end()
