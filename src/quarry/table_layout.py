"""Constrained-width text tables for quarry's CLI and MCP listings."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import ClassVar, Literal, Self, final

TABLE_WIDTH = 80


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column in a constrained-width table.

    Exactly one column per table should have ``fixed=False`` — it gets
    the remaining width budget and its content wraps when exceeded.
    """

    header: str
    min_width: int
    fixed: bool = True
    align: Literal["left", "right"] = "left"


@final
class TableLayout:
    r"""Render rows under a header, fitting :data:`TABLE_WIDTH` columns.

    Fixed columns grow to content width.  The single variable column
    (``fixed=False``) gets the remaining budget and wraps on overflow.
    Output is ``▶  HEADER\n   row\n   row...``.
    """

    __slots__ = ("_specs", "_variable")

    _COL_SEP: ClassVar[str] = "  "
    _HEADER_PREFIX: ClassVar[str] = "▶  "
    _ROW_PREFIX: ClassVar[str] = "   "
    _ANSI_RE: ClassVar[re.Pattern[str]] = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

    _specs: list[ColumnSpec]
    _variable: int | None

    def __new__(cls, specs: list[ColumnSpec]) -> Self:
        """Choose table columns and locate the optional variable-width column.

        Raises:
            ValueError: More than one column has variable width.
        """
        variable = [i for i, spec in enumerate(specs) if not spec.fixed]
        if len(variable) > 1:
            msg = "TableLayout: at most one variable column allowed"
            raise ValueError(msg)
        self = super().__new__(cls)
        self._specs = specs
        self._variable = variable[0] if variable else None
        return self

    @classmethod
    def visible_width(cls, text: str) -> int:
        """Printable width of *text*, ignoring ANSI escapes."""
        return len(cls._ANSI_RE.sub("", text))

    @staticmethod
    def truncate(text: str, max_len: int = 160) -> str:
        """Truncate *text* to *max_len* chars, collapsing whitespace."""
        collapsed = " ".join(text.split())
        if len(collapsed) <= max_len:
            return collapsed
        return collapsed[: max_len - 3].rstrip() + "..."

    def render(self, rows: list[list[str]]) -> str:
        """Return the header line followed by the data rows."""
        widths = self._widths(rows)
        header = self._line([spec.header for spec in self._specs], widths)
        title = self._HEADER_PREFIX + header[len(self._ROW_PREFIX) :]
        return "\n".join([title, *self._body(rows, widths)])

    def _widths(self, rows: list[list[str]]) -> list[int]:
        widths = [
            max(
                spec.min_width,
                len(spec.header),
                max((self.visible_width(row[i]) for row in rows), default=0),
            )
            for i, spec in enumerate(self._specs)
        ]
        if self._variable is not None:
            fixed_total = sum(w for i, w in enumerate(widths) if i != self._variable)
            separators = len(self._COL_SEP) * (len(self._specs) - 1)
            budget = TABLE_WIDTH - len(self._ROW_PREFIX) - fixed_total - separators
            widths[self._variable] = max(self._specs[self._variable].min_width, budget)
        return widths

    def _body(self, rows: list[list[str]], widths: list[int]) -> list[str]:
        if self._variable is None:
            return [self._line(row, widths) for row in rows]
        var = self._variable
        indent = " " * (
            len(self._ROW_PREFIX) + sum(widths[:var]) + len(self._COL_SEP) * var
        )
        out: list[str] = []
        for row in rows:
            first, *rest = textwrap.wrap(row[var], widths[var]) or [""]
            out.append(self._line([*row[:var], first, *row[var + 1 :]], widths))
            out.extend(indent + chunk for chunk in rest)
        return out

    def _line(self, cells: list[str], widths: list[int]) -> str:
        return self._ROW_PREFIX + self._COL_SEP.join(
            self._pad(cell, widths[i], self._specs[i].align)
            for i, cell in enumerate(cells)
        )

    @classmethod
    def _pad(cls, text: str, width: int, align: Literal["left", "right"]) -> str:
        padding = " " * max(0, width - cls.visible_width(text))
        return padding + text if align == "right" else text + padding
