"""Track the open Markdown code fence while scanning a file's lines in order."""

from __future__ import annotations

from typing import Self, final

__all__ = ["FenceScanner"]


@final
class FenceScanner:
    """Report which lines are shielded by a Markdown code fence, line by line.

    Feed lines top to bottom to :meth:`shields`. A fence opens on a non-indented
    run of three or more backticks or three or more tildes and closes only on a
    later non-indented run of the SAME character whose length is at least the
    opener's (CommonMark §4.5). Indented lines (a tab or four or more leading
    spaces) are code-block content and never change fence state. A line is
    *shielded* when it sits inside a fence or is itself a fence delimiter — such
    a line is code, not prose, so a Claude Code ``@``-import on it is inert.
    """

    __slots__ = ("_char", "_length")

    _char: str
    _length: int

    def __new__(cls) -> Self:
        """Start outside any Markdown fence with no opening delimiter recorded."""
        self = super().__new__(cls)
        self._char = ""
        self._length = 0
        return self

    @property
    def is_open(self) -> bool:
        """Return whether a fence is open — the next line would sit inside it.

        True after the scan when the file ends without closing its last fence
        (CommonMark closes it implicitly at EOF, so every trailing line is code).
        A caller appending a top-level line must refuse in that state: the line
        would land inside the code block, inert rather than top-level.
        """
        return bool(self._char)

    def shields(self, raw: str) -> bool:
        """Return whether *raw* is code, not a top-level line, advancing fence state."""
        indented = raw.startswith(("\t", "    "))
        if self._char:
            if not indented and self._closes(raw):
                self._char = ""
                self._length = 0
            return True
        if indented:
            return True
        opener = self._opens(raw)
        if opener is None:
            return False
        self._char, self._length = opener
        return True

    def _closes(self, raw: str) -> bool:
        """Return whether *raw* closes the open fence.

        A closing fence matches the open marker, is at least as long, and carries
        only trailing whitespace (CommonMark §4.5). An info string is valid on an
        opening fence only, so a backtick run followed by ``note`` never closes.
        """
        run = self._fence_run(raw)
        if run is None or run[0] != self._char or run[1] < self._length:
            return False
        rest = raw.rstrip("\r\n").lstrip(" ")[run[1] :]
        return not rest.strip()

    @staticmethod
    def _opens(raw: str) -> tuple[str, int] | None:  # None: not a fence opener
        """Return the opener ``(marker, length)`` if *raw* opens a fence, else ``None``.

        A backtick fence opener may not contain a backtick anywhere in its info
        string (CommonMark §4.5) — a line like ``` ```lang`x ``` is inline code,
        not a fence, so it must not shield an import. A tilde fence's info string
        is unrestricted, so backticks after ``~~~`` are fine.
        """
        run = FenceScanner._fence_run(raw)
        if run is None:
            return None
        marker, length = run
        if marker == "`" and "`" in raw.rstrip("\r\n").lstrip(" ")[length:]:
            return None
        return run

    @staticmethod
    def _fence_run(raw: str) -> tuple[str, int] | None:  # None: no fence run present
        """Return the ``(marker, length)`` of a leading fence run, else ``None``."""
        stripped = raw.rstrip("\r\n").lstrip(" ")
        for marker in ("`", "~"):
            run = len(stripped) - len(stripped.lstrip(marker))
            if run >= 3:
                return marker, run
        return None
