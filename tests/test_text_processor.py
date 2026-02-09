from __future__ import annotations

from pathlib import Path

import pytest

from quarry.models import PageType
from quarry.text_processor import (
    SUPPORTED_TEXT_EXTENSIONS,
    _read_text_with_fallback,
    process_raw_text,
    process_text_file,
)


class TestSupportedExtensions:
    def test_includes_expected(self):
        assert frozenset({".txt", ".md", ".tex", ".docx"}) == SUPPORTED_TEXT_EXTENSIONS


class TestReadTextWithFallback:
    def test_utf8_file(self, tmp_path: Path):
        f = tmp_path / "utf8.txt"
        f.write_text("Hello world", encoding="utf-8")
        assert _read_text_with_fallback(f) == "Hello world"

    def test_latin1_file(self, tmp_path: Path):
        f = tmp_path / "german.txt"
        f.write_bytes("Ärger mit Ü".encode("latin-1"))
        result = _read_text_with_fallback(f)
        assert "Ärger" in result
        assert "Ü" in result

    def test_cp1252_file(self, tmp_path: Path):
        f = tmp_path / "windows.txt"
        # 0x93/0x94 are left/right double quotes in CP1252
        f.write_bytes(b"\x93Hello\x94")
        result = _read_text_with_fallback(f)
        assert result == "\u201cHello\u201d"


class TestProcessTextFile:
    def test_plain_text_splits_on_blank_lines(self, tmp_path: Path):
        f = tmp_path / "notes.txt"
        f.write_text("First paragraph.\n\nSecond paragraph.\n\nThird paragraph.")

        pages = process_text_file(f)

        assert len(pages) == 3
        assert pages[0].text == "First paragraph."
        assert pages[1].text == "Second paragraph."
        assert pages[2].text == "Third paragraph."

    def test_plain_text_metadata(self, tmp_path: Path):
        f = tmp_path / "notes.txt"
        f.write_text("Hello.\n\nWorld.")

        pages = process_text_file(f)

        assert pages[0].document_name == "notes.txt"
        assert pages[0].document_path == str(f.resolve())
        assert pages[0].page_number == 1
        assert pages[0].total_pages == 2
        assert pages[0].page_type == PageType.SECTION

    def test_markdown_splits_on_headings(self, tmp_path: Path):
        f = tmp_path / "doc.md"
        f.write_text("# Intro\nSome text.\n\n## Details\nMore text.")

        pages = process_text_file(f)

        assert len(pages) == 2
        assert pages[0].text.startswith("# Intro")
        assert pages[1].text.startswith("## Details")

    def test_latex_splits_on_sections(self, tmp_path: Path):
        f = tmp_path / "paper.tex"
        f.write_text(
            "\\section{Intro}\nText here.\n\\subsection{Background}\nMore text."
        )

        pages = process_text_file(f)

        assert len(pages) == 2
        assert "\\section{Intro}" in pages[0].text
        assert "\\subsection{Background}" in pages[1].text

    def test_unsupported_extension_raises(self, tmp_path: Path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")

        with pytest.raises(ValueError, match="Unsupported text format"):
            process_text_file(f)

    def test_single_paragraph_produces_one_page(self, tmp_path: Path):
        f = tmp_path / "one.txt"
        f.write_text("Just one paragraph, no blank lines.")

        pages = process_text_file(f)

        assert len(pages) == 1
        assert pages[0].page_number == 1
        assert pages[0].total_pages == 1

    def test_empty_file_produces_no_pages(self, tmp_path: Path):
        f = tmp_path / "empty.txt"
        f.write_text("")

        pages = process_text_file(f)

        assert pages == []

    def test_latin1_file_processes_correctly(self, tmp_path: Path):
        f = tmp_path / "german.txt"
        f.write_bytes("Über die Brücke.\n\nZweiter Absatz.".encode("latin-1"))

        pages = process_text_file(f)

        assert len(pages) == 2
        assert "Über" in pages[0].text
        assert "Brücke" in pages[0].text

    def test_whitespace_only_sections_skipped(self, tmp_path: Path):
        f = tmp_path / "spacey.txt"
        f.write_text("Content.\n\n   \n\nMore content.")

        pages = process_text_file(f)

        assert len(pages) == 2


class TestProcessRawText:
    def test_auto_detects_markdown(self):
        pages = process_raw_text("# Title\nBody text.", "notes.md")

        assert len(pages) == 1
        assert pages[0].text.startswith("# Title")

    def test_auto_detects_latex(self):
        pages = process_raw_text("\\section{Intro}\nText.", "paper.tex")

        assert len(pages) == 1
        assert "\\section{Intro}" in pages[0].text

    def test_auto_detects_plain(self):
        pages = process_raw_text("First.\n\nSecond.", "notes.txt")

        assert len(pages) == 2

    def test_explicit_format_hint(self):
        text = "# Heading\nContent.\n\nParagraph two."
        pages = process_raw_text(text, "doc.txt", format_hint="plain")

        # With plain hint, splits on blank lines, not headings
        assert len(pages) == 2

    def test_document_path_is_string_marker(self):
        pages = process_raw_text("Hello.", "test.txt")

        assert pages[0].document_path == "<string>"

    def test_empty_text_produces_no_pages(self):
        pages = process_raw_text("", "empty.txt")

        assert pages == []


class TestDocxProcessing:
    def _make_docx(self, path: Path, paragraphs: list[tuple[str, str]]) -> None:
        """Create a DOCX file with (style_name, text) paragraphs."""
        import docx

        doc = docx.Document()
        for style, text in paragraphs:
            doc.add_paragraph(text, style=style)
        doc.save(str(path))

    def test_splits_on_headings(self, tmp_path: Path):
        f = tmp_path / "doc.docx"
        self._make_docx(
            f,
            [
                ("Heading 1", "Introduction"),
                ("Normal", "First paragraph."),
                ("Heading 2", "Details"),
                ("Normal", "Second paragraph."),
            ],
        )

        pages = process_text_file(f)

        assert len(pages) == 2
        assert "Introduction" in pages[0].text
        assert "First paragraph." in pages[0].text
        assert "Details" in pages[1].text
        assert "Second paragraph." in pages[1].text

    def test_heading_text_preserved(self, tmp_path: Path):
        f = tmp_path / "doc.docx"
        self._make_docx(
            f,
            [
                ("Heading 1", "Chapter One"),
                ("Normal", "Content."),
            ],
        )

        pages = process_text_file(f)

        assert len(pages) == 1
        assert pages[0].text.startswith("Chapter One")

    def test_no_headings_single_section(self, tmp_path: Path):
        f = tmp_path / "doc.docx"
        self._make_docx(
            f,
            [
                ("Normal", "Line one."),
                ("Normal", "Line two."),
            ],
        )

        pages = process_text_file(f)

        assert len(pages) == 1
        assert "Line one." in pages[0].text
        assert "Line two." in pages[0].text

    def test_metadata(self, tmp_path: Path):
        f = tmp_path / "report.docx"
        self._make_docx(f, [("Normal", "Content.")])

        pages = process_text_file(f)

        assert pages[0].document_name == "report.docx"
        assert pages[0].page_type == PageType.SECTION
