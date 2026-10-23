"""A value an outside party chose, rendered for a line of text and for a command."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Self, final

from quarry.log_redactor import LogRedactor


@final
@dataclass(frozen=True, slots=True)
class Shown:
    """A directory or collection name in its two safe forms.

    ``text`` is for prose: quoted on one line, so nothing in it can start a new
    line or read as an instruction.  ``arg`` is for a command line someone may
    paste: ``shlex``-quoted, so a quote, ``$()`` or a backtick stays one argument,
    and on one line.  A template takes a ``Shown`` and never the raw value.
    """

    text: str
    arg: str

    @classmethod
    def of(cls, value: object) -> Self:
        """Return the two forms of ``str(value)``."""
        raw = str(value)
        return cls(LogRedactor.quoted(raw), LogRedactor.one_line(shlex.quote(raw)))
