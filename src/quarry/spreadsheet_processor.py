"""Spreadsheet processing: XLSX and CSV to LaTeX tabular sections."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

from quarry.models import PageContent, PageType
from quarry.text_processor import _read_text_with_fallback, _sections_to_pages

logger = logging.getLogger(__name__)

SUPPORTED_SPREADSHEET_EXTENSIONS = frozenset({".xlsx", ".csv"})

# Characters that must be escaped in LaTeX tabular cells.
_LATEX_SPECIAL = str.maketrans(
    {
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
        "\\": r"\textbackslash{}",
    }
)


def _escape_latex(text: str) -> str:
    """Escape LaTeX special characters in a cell value."""
    return text.translate(_LATEX_SPECIAL)


def _rows_to_latex(
    headers: list[str],
    rows: list[list[str]],
    sheet_name: str | None = None,
) -> str:
    """Render headers + data rows as a LaTeX tabular block.

    Returns an empty string when *headers* is empty.
    """
    if not headers:
        return ""

    ncols = len(headers)
    col_spec = "l" * ncols

    lines: list[str] = []
    if sheet_name:
        lines.append(f"% Sheet: {sheet_name}")
    lines.append(f"\\begin{{tabular}}{{{col_spec}}}")
    lines.append("\\hline")
    lines.append(" & ".join(_escape_latex(h) for h in headers) + " \\\\")
    lines.append("\\hline")

    for row in rows:
        padded = row[:ncols] + [""] * max(0, ncols - len(row))
        lines.append(" & ".join(_escape_latex(c) for c in padded) + " \\\\")

    lines.append("\\hline")
    lines.append("\\end{tabular}")

    return "\n".join(lines)


def _split_rows_to_sections(
    headers: list[str],
    rows: list[list[str]],
    sheet_name: str | None,
    max_chars: int,
) -> list[str]:
    """Split a large table into row-group sections with repeated headers.

    If the full table fits within *max_chars*, returns a single section.
    Otherwise, rows are grouped so each section stays under the limit.
    """
    full = _rows_to_latex(headers, rows, sheet_name)
    if len(full) <= max_chars or len(rows) <= 1:
        return [full] if full.strip() else []

    sections: list[str] = []
    current_rows: list[list[str]] = []

    for row in rows:
        candidate = [*current_rows, row]
        block = _rows_to_latex(headers, candidate, sheet_name)
        if len(block) > max_chars and current_rows:
            sections.append(_rows_to_latex(headers, current_rows, sheet_name))
            current_rows = [row]
        else:
            current_rows.append(row)

    if current_rows:
        sections.append(_rows_to_latex(headers, current_rows, sheet_name))

    return sections


def _read_xlsx(
    file_path: Path,
) -> list[tuple[str, list[str], list[list[str]]]]:
    """Read an XLSX workbook into ``(sheet_name, headers, rows)`` tuples.

    Uses ``data_only=True`` so formulas resolve to their cached values.
    Merged cells yield their top-left value; other cells in the merge
    are empty strings.
    """
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


def _read_csv(
    file_path: Path,
) -> list[tuple[str, list[str], list[list[str]]]]:
    """Read a CSV file into a single ``(name, headers, rows)`` tuple."""
    text = _read_text_with_fallback(file_path)
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)

    if not all_rows:
        return []

    headers = all_rows[0]
    data = all_rows[1:]
    return [(file_path.stem, headers, data)]


def process_spreadsheet_file(
    file_path: Path,
    *,
    max_chars: int = 1800,
    document_name: str | None = None,
) -> tuple[list[PageContent], int]:
    """Read a spreadsheet and convert each sheet to LaTeX tabular sections.

    Each worksheet becomes one or more sections.  Large sheets are split
    into row groups with column headers repeated in each section.

    Args:
        file_path: Path to spreadsheet file (``.xlsx`` or ``.csv``).
        max_chars: Maximum characters per section before row-group splitting.
        document_name: Override for the stored document name. Defaults to
            ``file_path.name``.

    Returns:
        Tuple of (pages, sheet_count) where pages is a list of PageContent
        objects and sheet_count is the number of worksheets/files processed.

    Raises:
        ValueError: If file extension is not a supported spreadsheet format.
    """
    suffix = file_path.suffix.lower()

    if suffix == ".xlsx":
        raw_sheets = _read_xlsx(file_path)
    elif suffix == ".csv":
        raw_sheets = _read_csv(file_path)
    else:
        msg = f"Unsupported spreadsheet format: {suffix}"
        raise ValueError(msg)

    multi_sheet = len(raw_sheets) > 1
    sheet_count = len(raw_sheets)
    sections: list[str] = []

    for sheet_name, headers, rows in raw_sheets:
        if not headers:
            continue
        name = sheet_name if multi_sheet else None
        sheet_sections = _split_rows_to_sections(headers, rows, name, max_chars)
        sections.extend(sheet_sections)

    if not sections:
        return [], sheet_count

    resolved_name = document_name or file_path.name
    document_path = str(file_path.resolve())
    pages = _sections_to_pages(
        sections, resolved_name, document_path, PageType.SPREADSHEET
    )
    return pages, sheet_count
