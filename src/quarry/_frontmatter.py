"""Stdlib-only parser for the YAML-ish frontmatter of a Markdown config file.

``Frontmatter`` reads the lines between the leading ``---`` fences once and
answers ``block(name)`` queries against them.  It understands only the minimal
nested subset quarry config uses — ``<block>:`` followed by indented
``key: value`` lines, with inline ``# ...`` comments stripped — and depends on
no third-party YAML library, so hook entry points can import it without pulling
in the heavy pipeline dependencies.
"""

from __future__ import annotations

from typing import Self, final


@final
class Frontmatter:
    """The frontmatter fence block of a Markdown config file's text."""

    __slots__ = ("_lines",)

    _lines: list[str]

    def __new__(cls, text: str) -> Self:
        self = super().__new__(cls)
        self._lines = cls._between_fences(text)
        return self

    def block(self, name: str) -> dict[str, str] | None:
        """Return ``key: value`` pairs under ``<name>:`` (None when absent).

        None means the block is absent; an empty dict means the block is
        present but has no fields.
        """
        result: dict[str, str] = {}
        in_block = False
        header = f"{name}:"
        for line in self._lines:
            stripped = line.strip()
            if stripped == header:
                in_block = True
                continue
            if in_block:
                if not stripped:
                    continue
                if not line.startswith((" ", "\t")):
                    break
                if ":" in stripped:
                    key, _, val = stripped.partition(":")
                    result[key.strip()] = val.split("#")[0].strip()
        return result if in_block else None

    @staticmethod
    def _between_fences(text: str) -> list[str]:
        """Return the lines between the leading ``---`` fences (empty if none)."""
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return []
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return lines[1:i]
        return []
