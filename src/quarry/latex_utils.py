"""LaTeX table serialization: escaping and tabular generation."""

from __future__ import annotations

from typing import final


@final
class LatexSerializer:
    """Serialize data to LaTeX tabular format with proper escaping."""

    # Characters that must be escaped in LaTeX tabular cells.
    _SPECIAL = str.maketrans(
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

    @staticmethod
    def escape(text: str) -> str:
        """Escape LaTeX special characters in a cell value."""
        return text.translate(LatexSerializer._SPECIAL)

    @staticmethod
    def serialize_table(
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
        lines.append(" & ".join(LatexSerializer.escape(h) for h in headers) + " \\\\")
        lines.append("\\hline")

        for row in rows:
            padded = row[:ncols] + [""] * max(0, ncols - len(row))
            lines.append(
                " & ".join(LatexSerializer.escape(c) for c in padded) + " \\\\"
            )

        lines.append("\\hline")
        lines.append("\\end{tabular}")

        return "\n".join(lines)
