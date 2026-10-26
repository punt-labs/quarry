"""Unit-file and env-file text, read the way systemd reads it."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, final

if TYPE_CHECKING:
    from collections.abc import Iterator


@final
class SystemdText:
    """Read settings out of a unit or env file's text as systemd does.

    Lines are split on a newline only, a line ending in a backslash is joined
    with the next, and ``#`` and ``;`` comment lines are skipped.  Every read is
    one pass with no backtracking, so a hostile file costs time linear in its
    size.
    """

    _NUMBER: ClassVar[str] = r"(?<!\d)\d{1,9}(?:\.\d{1,9})?(?!\d)"
    _TERM: ClassVar[re.Pattern[str]] = re.compile(rf"({_NUMBER})\s*([a-z]*)")
    # The keys that set a unit's stop timeout, the last assignment of either wins.
    _STOP_KEYS: ClassVar[frozenset[str]] = frozenset({"TimeoutStopSec", "TimeoutSec"})
    # systemd time-span units, in seconds; a bare number is seconds.
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
            if section == "Service" and separator and key.strip() in cls._STOP_KEYS:
                assigned = value.strip()
        return assigned

    @classmethod
    def assignment(cls, text: str, name: str) -> str | None:
        """Return the last value *name* is given in *text*, unquoted, else ``None``.

        ``None`` is the documented answer for a name the text never sets.
        """
        found: str | None = None
        for line in cls._logical_lines(text):
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                found = value.strip().strip('"')
        return found

    @staticmethod
    def _logical_lines(text: str) -> Iterator[str]:
        """Yield the logical lines of a unit or env file, as systemd reads them.

        Split on ``\\n`` only (a carriage return or a form feed is not a line
        break), a line ending in a backslash joined with the next, blank lines and
        ``#`` or ``;`` comments skipped.  One pass with no backtracking.
        """
        pending = ""
        for physical in text.split("\n"):
            line = pending + physical.strip()
            if line.endswith("\\"):
                pending = line[:-1] + " "
                continue
            pending = ""
            if line and line[0] not in "#;":
                yield line
        if pending.strip():
            yield pending.strip()

    @classmethod
    def timespan(cls, text: str) -> float:
        """Return a systemd time span in seconds.

        Terms are read left to right, each a number and an optional unit, with
        nothing skipped but whitespace.

        Raises:
            ValueError: *text* is empty, has a term that is not a number and a
                known unit, or has anything after the last term.
        """
        lowered = text.lower()
        position = cls._skip_space(lowered, 0)
        total, terms = 0.0, 0
        while position < len(lowered):
            seconds, end = cls._term(lowered, position)
            total, terms = total + seconds, terms + 1
            position = cls._skip_space(lowered, end)
        if terms == 0:
            msg = "not a systemd time span: empty"
            raise ValueError(msg)
        return total

    @staticmethod
    def _skip_space(text: str, position: int) -> int:
        """Return the index of the first non-space character at or after *position*."""
        while position < len(text) and text[position].isspace():
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
