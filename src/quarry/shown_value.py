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
    and on one line.  A value with a character that is not printable (a line
    separator, an escape, a zero-width or tag character) is never emitted raw in
    ``arg``, however it is quoted: ``arg`` is the *fallback* instead, because a
    terminal or a model reads such characters as something other than the name.
    A template takes a ``Shown`` and never the raw value.
    """

    text: str
    arg: str

    @classmethod
    def of(cls, value: object, *, fallback: str = "<name>") -> Self:
        """Return the two forms of ``str(value)``; *fallback* if it is unprintable."""
        raw = str(value)
        arg = shlex.quote(raw) if raw.isprintable() else shlex.quote(fallback)
        return cls(LogRedactor.quoted(raw), arg)
