"""Spreadsheet tables: parse CSV under the page budget, split rows into sections."""

from __future__ import annotations

import csv
import io
from typing import final

from quarry.content_guard import UnindexableContentError
from quarry.db.row_budget import RowBudget
from quarry.latex_utils import LatexSerializer

# The csv module's field limit is process-global, so it is set exactly once, here,
# to a fixed value that is only ever above the stdlib default.  An idempotent
# assignment cannot race; a per-parse set and restore would, across sync workers.
csv.field_size_limit(RowBudget().max_page_bytes)


@final
class SheetTables:
    """Tables read from a spreadsheet, ready to become sections.

    CSV text parses with the ``csv`` module's field limit set on purpose.

    The stdlib refuses a field over 128 KiB, which a legitimate wide cell can
    exceed.  The limit is raised, once at import, only as far as one stored page
    may be, not to ``sys.maxsize``, so a hostile file cannot make the parser hold
    an unbounded field.  A field that still does not fit is a refusal with fixed,
    path-free text, so it is a per-file refusal in sync and a quiet task failure
    elsewhere, never a crash.
    """

    __slots__ = ()

    @staticmethod
    def parse_csv(text: str) -> list[list[str]]:
        """Return every row of *text*.

        Raises:
            UnindexableContentError: A field exceeds the page budget, or the
                text is not parseable CSV.
        """
        try:
            return list(csv.reader(io.StringIO(text)))
        except csv.Error as exc:
            is_oversize = "field larger than field limit" in str(exc)
            msg = (
                "content rejected: CSV field exceeds the page budget"
                if is_oversize
                else "content rejected: not parseable CSV"
            )
            raise UnindexableContentError(msg) from None

    @staticmethod
    def sections(
        headers: list[str],
        rows: list[list[str]],
        sheet_name: str | None,
        max_chars: int,
    ) -> list[str]:
        """Split a large table into row-group sections with repeated headers."""
        full = LatexSerializer.serialize_table(headers, rows, sheet_name)
        if len(full) <= max_chars or len(rows) <= 1:
            return [full] if full.strip() else []

        sections: list[str] = []
        current_rows: list[list[str]] = []

        for row in rows:
            candidate = [*current_rows, row]
            block = LatexSerializer.serialize_table(headers, candidate, sheet_name)
            if len(block) > max_chars and current_rows:
                section = LatexSerializer.serialize_table(
                    headers,
                    current_rows,
                    sheet_name,
                )
                sections.append(section)
                current_rows = [row]
            else:
                current_rows.append(row)

        if current_rows:
            section = LatexSerializer.serialize_table(
                headers,
                current_rows,
                sheet_name,
            )
            sections.append(section)

        return sections
