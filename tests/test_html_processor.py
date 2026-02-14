from __future__ import annotations

from pathlib import Path

import pytest

from quarry.html_processor import (
    SUPPORTED_HTML_EXTENSIONS,
    _extract_title,
    _has_markdown_headings,
    _html_to_markdown,
    _strip_boilerplate,
    process_html_file,
)
from quarry.models import PageType


class TestSupportedExtensions:
    def test_includes_html_and_htm(self):
        assert ".html" in SUPPORTED_HTML_EXTENSIONS
        assert ".htm" in SUPPORTED_HTML_EXTENSIONS

    def test_no_overlap_with_other_extensions(self):
        from quarry.code_processor import SUPPORTED_CODE_EXTENSIONS
        from quarry.spreadsheet_processor import SUPPORTED_SPREADSHEET_EXTENSIONS
        from quarry.text_processor import SUPPORTED_TEXT_EXTENSIONS

        overlap = SUPPORTED_HTML_EXTENSIONS & (
            SUPPORTED_CODE_EXTENSIONS
            | SUPPORTED_TEXT_EXTENSIONS
            | SUPPORTED_SPREADSHEET_EXTENSIONS
        )
        assert overlap == frozenset(), f"Overlapping extensions: {overlap}"


class TestStripBoilerplate:
    def test_removes_script_tags(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            "<body><p>Keep</p><script>alert(1)</script></body>", "html.parser"
        )
        _strip_boilerplate(soup)
        assert soup.find("script") is None
        assert "Keep" in soup.get_text()

    def test_removes_all_boilerplate_tags(self):
        from bs4 import BeautifulSoup

        html = (
            "<body>"
            "<nav>Menu</nav>"
            "<header>Banner</header>"
            "<main><p>Content</p></main>"
            "<aside>Sidebar</aside>"
            "<footer>Copyright</footer>"
            "<style>.x{}</style>"
            "<form><input></form>"
            "<noscript>Enable JS</noscript>"
            "</body>"
        )
        soup = BeautifulSoup(html, "html.parser")
        _strip_boilerplate(soup)

        text = soup.get_text()
        assert "Content" in text
        assert "Menu" not in text
        assert "Banner" not in text
        assert "Sidebar" not in text
        assert "Copyright" not in text
        assert "Enable JS" not in text

    def test_preserves_article_content(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            "<article><h1>Title</h1><p>Body text.</p></article>", "html.parser"
        )
        _strip_boilerplate(soup)
        assert "Title" in soup.get_text()
        assert "Body text." in soup.get_text()


class TestExtractTitle:
    def test_extracts_title(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(
            "<html><head><title>My Page</title></head></html>", "html.parser"
        )
        assert _extract_title(soup) == "My Page"

    def test_strips_whitespace(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<title>  Spaced Title  </title>", "html.parser")
        assert _extract_title(soup) == "Spaced Title"

    def test_no_title_returns_empty(self):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<html><body>No title</body></html>", "html.parser")
        assert _extract_title(soup) == ""


class TestHtmlToMarkdown:
    def test_headings(self):
        md = _html_to_markdown("<h1>Title</h1><h2>Subtitle</h2>")
        assert "# Title" in md
        assert "## Subtitle" in md

    def test_paragraphs(self):
        md = _html_to_markdown("<p>First</p><p>Second</p>")
        assert "First" in md
        assert "Second" in md

    def test_lists(self):
        md = _html_to_markdown("<ul><li>One</li><li>Two</li></ul>")
        assert "- One" in md
        assert "- Two" in md

    def test_tables(self):
        md = _html_to_markdown(
            "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
        )
        assert "A" in md
        assert "B" in md
        assert "1" in md
        assert "2" in md


class TestHasMarkdownHeadings:
    def test_detects_h1(self):
        assert _has_markdown_headings("# Title\nBody") is True

    def test_detects_h2(self):
        assert _has_markdown_headings("## Subtitle\nBody") is True

    def test_no_headings(self):
        assert _has_markdown_headings("Just plain text\nAnother line") is False

    def test_hash_in_middle_not_heading(self):
        assert _has_markdown_headings("Issue #42 is open") is False


class TestProcessHTMLFile:
    def test_basic_html_with_headings(self, tmp_path: Path):
        f = tmp_path / "doc.html"
        f.write_text(
            "<html><body>"
            "<h1>Introduction</h1><p>First section.</p>"
            "<h2>Details</h2><p>Second section.</p>"
            "</body></html>"
        )

        pages = process_html_file(f)

        assert len(pages) >= 2
        texts = [p.text for p in pages]
        full = " ".join(texts)
        assert "Introduction" in full
        assert "Details" in full

    def test_metadata(self, tmp_path: Path):
        f = tmp_path / "article.html"
        f.write_text("<html><body><h1>Title</h1><p>Content.</p></body></html>")

        pages = process_html_file(f)

        assert pages[0].document_name == "article.html"
        assert pages[0].document_path == str(f.resolve())
        assert pages[0].page_number == 1
        assert pages[0].page_type == PageType.SECTION

    def test_page_numbers_sequential(self, tmp_path: Path):
        f = tmp_path / "multi.html"
        f.write_text(
            "<html><body>"
            "<h1>One</h1><p>A</p>"
            "<h1>Two</h1><p>B</p>"
            "<h1>Three</h1><p>C</p>"
            "</body></html>"
        )

        pages = process_html_file(f)

        assert len(pages) >= 3
        for i, page in enumerate(pages):
            assert page.page_number == i + 1
            assert page.total_pages == len(pages)

    def test_boilerplate_stripped(self, tmp_path: Path):
        f = tmp_path / "page.html"
        f.write_text(
            "<html><body>"
            "<nav><a href='/'>Home</a></nav>"
            "<article><p>Real content here.</p></article>"
            "<footer>Copyright 2026</footer>"
            "<script>tracking();</script>"
            "</body></html>"
        )

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "Real content" in full
        assert "Home" not in full
        assert "Copyright" not in full
        assert "tracking" not in full

    def test_title_prepended(self, tmp_path: Path):
        f = tmp_path / "titled.html"
        f.write_text(
            "<html><head><title>My Article</title></head>"
            "<body><p>Body text only, no headings.</p></body></html>"
        )

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "My Article" in full
        assert "Body text" in full

    def test_no_headings_falls_back_to_plain_split(self, tmp_path: Path):
        f = tmp_path / "plain.html"
        f.write_text(
            "<html><body>"
            "<p>First paragraph of content.</p>"
            "<p>Second paragraph of content.</p>"
            "</body></html>"
        )

        pages = process_html_file(f)

        assert len(pages) >= 1
        full = " ".join(p.text for p in pages)
        assert "First paragraph" in full
        assert "Second paragraph" in full

    def test_htm_extension(self, tmp_path: Path):
        f = tmp_path / "legacy.htm"
        f.write_text("<html><body><p>Works with .htm</p></body></html>")

        pages = process_html_file(f)

        assert len(pages) >= 1
        assert "Works with .htm" in pages[0].text

    def test_html_entities_decoded(self, tmp_path: Path):
        f = tmp_path / "entities.html"
        f.write_text("<html><body><p>Price: &lt;$100 &amp; worth it</p></body></html>")

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "<$100" in full
        assert "& worth it" in full

    def test_html_with_tables(self, tmp_path: Path):
        f = tmp_path / "table.html"
        f.write_text(
            "<html><body>"
            "<table><tr><th>Name</th><th>Age</th></tr>"
            "<tr><td>Alice</td><td>30</td></tr></table>"
            "</body></html>"
        )

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "Alice" in full
        assert "30" in full

    def test_document_name_override(self, tmp_path: Path):
        f = tmp_path / "page.html"
        f.write_text("<html><body><p>Content</p></body></html>")

        pages = process_html_file(f, document_name="subdir/page.html")

        assert pages[0].document_name == "subdir/page.html"

    def test_special_chars_in_content(self, tmp_path: Path):
        f = tmp_path / "special.html"
        f.write_text("<html><body><p>Revenue was $4.2M (12% growth)</p></body></html>")

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "$4.2M" in full
        assert "12%" in full


class TestProcessHTMLFileEdgeCases:
    def test_empty_html(self, tmp_path: Path):
        f = tmp_path / "empty.html"
        f.write_text("")

        pages = process_html_file(f)

        assert pages == []

    def test_only_boilerplate(self, tmp_path: Path):
        f = tmp_path / "boilerplate.html"
        f.write_text(
            "<html><body>"
            "<nav>Menu</nav><footer>Footer</footer>"
            "<script>code()</script>"
            "</body></html>"
        )

        pages = process_html_file(f)

        assert pages == []

    def test_whitespace_only_body(self, tmp_path: Path):
        f = tmp_path / "whitespace.html"
        f.write_text("<html><body>   \n\n   </body></html>")

        pages = process_html_file(f)

        assert pages == []

    def test_malformed_html(self, tmp_path: Path):
        f = tmp_path / "broken.html"
        f.write_text("<p>Unclosed paragraph<div>Mixed tags</p></div>Extra")

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "Unclosed paragraph" in full

    def test_xhtml_declaration(self, tmp_path: Path):
        f = tmp_path / "xhtml.html"
        f.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN">\n'
            "<html><body><p>XHTML content</p></body></html>"
        )

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "XHTML content" in full

    def test_encoding_fallback(self, tmp_path: Path):
        f = tmp_path / "cp1252.html"
        f.write_bytes(b"<html><body><p>\x93Smart quotes\x94 here</p></body></html>")

        pages = process_html_file(f)

        full = " ".join(p.text for p in pages)
        assert "Smart quotes" in full


class TestProcessHTMLFileErrors:
    def test_unsupported_extension(self, tmp_path: Path):
        f = tmp_path / "data.xml"
        f.write_text("<root>data</root>")

        with pytest.raises(ValueError, match="Unsupported HTML format"):
            process_html_file(f)
