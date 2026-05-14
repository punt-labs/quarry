from __future__ import annotations

from quarry.latex_utils import LatexSerializer


class TestEscapeLatex:
    def test_escapes_ampersand(self):
        assert LatexSerializer.escape("A & B") == r"A \& B"

    def test_escapes_percent(self):
        assert LatexSerializer.escape("50%") == r"50\%"

    def test_escapes_dollar(self):
        assert LatexSerializer.escape("$100") == r"\$100"

    def test_escapes_hash(self):
        assert LatexSerializer.escape("#1") == r"\#1"

    def test_escapes_underscore(self):
        assert LatexSerializer.escape("my_var") == r"my\_var"

    def test_escapes_braces(self):
        assert LatexSerializer.escape("{x}") == r"\{x\}"

    def test_escapes_backslash(self):
        assert LatexSerializer.escape(r"a\b") == r"a\textbackslash{}b"

    def test_plain_text_unchanged(self):
        assert LatexSerializer.escape("Hello World 123") == "Hello World 123"

    def test_multiple_specials(self):
        result = LatexSerializer.escape("$100 & 50%")
        assert r"\$" in result
        assert r"\&" in result
        assert r"\%" in result


class TestRowsToLatex:
    def test_basic_table(self):
        result = LatexSerializer.serialize_table(["Name", "Age"], [["Alice", "30"]])
        assert r"\begin{tabular}{ll}" in result
        assert "Name & Age" in result
        assert "Alice & 30" in result
        assert r"\end{tabular}" in result

    def test_empty_headers_returns_empty(self):
        assert LatexSerializer.serialize_table([], [["a", "b"]]) == ""

    def test_sheet_name_prefix(self):
        result = LatexSerializer.serialize_table(["A"], [["1"]], sheet_name="Data")
        assert "% Sheet: Data" in result

    def test_no_sheet_name(self):
        result = LatexSerializer.serialize_table(["A"], [["1"]])
        assert "% Sheet" not in result

    def test_row_padding(self):
        result = LatexSerializer.serialize_table(["A", "B", "C"], [["1"]])
        # Row should be padded to match 3 columns
        assert "1 &  & " in result

    def test_row_truncation(self):
        result = LatexSerializer.serialize_table(["A"], [["1", "2", "3"]])
        # Extra columns should be dropped
        assert "1 \\\\" in result
        assert "2" not in result

    def test_latex_escaping_in_cells(self):
        result = LatexSerializer.serialize_table(["Price"], [["$100"]])
        assert r"\$100" in result

    def test_empty_rows(self):
        result = LatexSerializer.serialize_table(["A", "B"], [])
        assert r"\begin{tabular}" in result
        assert r"\end{tabular}" in result
