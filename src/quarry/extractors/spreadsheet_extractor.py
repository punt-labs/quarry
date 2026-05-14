"""Spreadsheet format extraction: XLSX and CSV to LaTeX tabular sections."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path
from typing import Self

from quarry.ingestion.text_splitter import read_text_with_fallback, sections_to_pages
from quarry.latex_utils import LatexSerializer
from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)

SUPPORTED_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".csv"})


class SpreadsheetExtractor:
    """Extract pages from spreadsheet files (XLSX, CSV).

    Implements ``FormatExtractor`` protocol.  Each worksheet becomes one
    or more sections.  Large sheets are split into row groups with column
    headers repeated in each section.

    The ``max_chars`` parameter controls the maximum characters per section
    before row-group splitting occurs.
    """

    _max_chars: int

    def __new__(cls, *, max_chars: int = 1800) -> Self:
        self = super().__new__(cls)
        self._max_chars = max_chars
        return self

    def extract_pages(
        self,
        path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Read a spreadsheet and convert each sheet to LaTeX tabular sections.

        Returns:
            List of PageContent objects.  Empty list for empty files.

        Raises:
            ValueError: If file extension is not a supported spreadsheet format.
        """
        suffix = path.suffix.lower()

        if suffix == ".xlsx":
            raw_sheets = self._read_xlsx(path)
        elif suffix == ".csv":
            raw_sheets = self._read_csv(path)
        else:
            msg = f"Unsupported spreadsheet format: {suffix}"
            raise ValueError(msg)

        multi_sheet = len(raw_sheets) > 1
        sections: list[str] = []

        for sheet_name, headers, rows in raw_sheets:
            if not headers:
                continue
            name = sheet_name if multi_sheet else None
            sheet_sections = self._split_rows_to_sections(
                headers, rows, name, self._max_chars
            )
            sections.extend(sheet_sections)

        if not sections:
            return []

        resolved_name = document_name or path.name
        document_path = str(path.resolve())
        return sections_to_pages(
            sections, resolved_name, document_path, PageType.SPREADSHEET
        )

    @staticmethod
    def _split_rows_to_sections(
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

    @staticmethod
    def _read_xlsx(
        file_path: Path,
    ) -> list[tuple[str, list[str], list[list[str]]]]:
        """Read an XLSX workbook into ``(sheet_name, headers, rows)`` tuples."""
        import openpyxl  # noqa: PLC0415

        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        sheets: list[tuple[str, list[str], list[list[str]]]] = []

        try:
            for ws in wb.worksheets:
                all_rows = [
                    [str(cell) if cell is not None else "" for cell in row]
                    for row in ws.iter_rows(values_only=True)
                ]

                if not all_rows:
                    continue

                headers = all_rows[0]
                data = all_rows[1:]
                sheets.append((ws.title, headers, data))
        finally:
            wb.close()

        return sheets

    @staticmethod
    def _read_csv(
        file_path: Path,
    ) -> list[tuple[str, list[str], list[list[str]]]]:
        """Read a CSV file into a single ``(name, headers, rows)`` tuple."""
        text = read_text_with_fallback(file_path)
        reader = csv.reader(io.StringIO(text))
        all_rows = list(reader)

        if not all_rows:
            return []

        headers = all_rows[0]
        data = all_rows[1:]
        return [(file_path.stem, headers, data)]
