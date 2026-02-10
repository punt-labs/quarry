from __future__ import annotations

from pathlib import Path

import pytest

from quarry.code_processor import (
    SUPPORTED_CODE_EXTENSIONS,
    _fallback_split,
    _split_with_treesitter,
    process_code_file,
)
from quarry.models import PageType


class TestSupportedExtensions:
    def test_includes_major_languages(self):
        expected = {".py", ".js", ".ts", ".java", ".c", ".cpp", ".go", ".rs", ".rb"}
        assert expected <= SUPPORTED_CODE_EXTENSIONS

    def test_no_overlap_with_text_extensions(self):
        from quarry.text_processor import SUPPORTED_TEXT_EXTENSIONS

        overlap = SUPPORTED_CODE_EXTENSIONS & SUPPORTED_TEXT_EXTENSIONS
        assert overlap == frozenset(), f"Overlapping extensions: {overlap}"


class TestProcessCodeFile:
    def test_python_functions(self, tmp_path: Path):
        f = tmp_path / "example.py"
        f.write_text(
            "import os\n\n\ndef foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
        )

        pages = process_code_file(f)

        # tree-sitter: imports group + 2 functions = 3 sections
        assert len(pages) == 3
        assert "import os" in pages[0].text
        assert "def foo" in pages[1].text
        assert "def bar" in pages[2].text

    def test_python_class(self, tmp_path: Path):
        f = tmp_path / "cls.py"
        f.write_text(
            "class Greeter:\n"
            "    def __init__(self, name: str):\n"
            "        self.name = name\n"
            "\n"
            "    def greet(self) -> str:\n"
            '        return f"Hello, {self.name}"\n'
        )

        pages = process_code_file(f)

        assert len(pages) == 1
        assert "class Greeter" in pages[0].text
        # Methods stay inside the class — not split out
        assert "def greet" in pages[0].text

    def test_metadata(self, tmp_path: Path):
        f = tmp_path / "meta.py"
        f.write_text("def only():\n    pass\n")

        pages = process_code_file(f)

        assert pages[0].document_name == "meta.py"
        assert pages[0].document_path == str(f.resolve())
        assert pages[0].page_type == PageType.CODE
        assert pages[0].page_number == 1

    def test_empty_file(self, tmp_path: Path):
        f = tmp_path / "empty.py"
        f.write_text("")

        assert process_code_file(f) == []

    def test_whitespace_only(self, tmp_path: Path):
        f = tmp_path / "blank.py"
        f.write_text("   \n\n  \n")

        assert process_code_file(f) == []

    def test_unsupported_extension(self, tmp_path: Path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")

        with pytest.raises(ValueError, match="Unsupported code format"):
            process_code_file(f)

    def test_javascript_functions(self, tmp_path: Path):
        f = tmp_path / "app.js"
        f.write_text(
            "function add(a, b) {\n  return a + b;\n}\n\n"
            "function sub(a, b) {\n  return a - b;\n}\n"
        )

        pages = process_code_file(f)

        assert len(pages) == 2
        assert "function add" in pages[0].text
        assert "function sub" in pages[1].text

    def test_javascript_constants_grouped(self, tmp_path: Path):
        f = tmp_path / "config.js"
        f.write_text(
            'const API_URL = "https://example.com";\n'
            "const TIMEOUT = 5000;\n\n"
            "function fetchData() {\n  return fetch(API_URL);\n}\n"
        )

        pages = process_code_file(f)

        assert len(pages) == 2
        assert "API_URL" in pages[0].text
        assert "TIMEOUT" in pages[0].text
        assert "function fetchData" in pages[1].text

    def test_rust_items(self, tmp_path: Path):
        f = tmp_path / "lib.rs"
        f.write_text(
            "struct Point {\n    x: f64,\n    y: f64,\n}\n\n"
            "fn distance(a: &Point, b: &Point) -> f64 {\n"
            "    ((a.x - b.x).powi(2) + (a.y - b.y).powi(2)).sqrt()\n"
            "}\n"
        )

        pages = process_code_file(f)

        assert len(pages) == 2
        assert "struct Point" in pages[0].text
        assert "fn distance" in pages[1].text

    def test_go_functions(self, tmp_path: Path):
        f = tmp_path / "main.go"
        f.write_text(
            "package main\n\n"
            'import "fmt"\n\n'
            'func main() {\n\tfmt.Println("hello")\n}\n\n'
            "func add(a, b int) int {\n\treturn a + b\n}\n"
        )

        pages = process_code_file(f)

        assert len(pages) >= 3
        texts = [p.text for p in pages]
        assert any("func main" in t for t in texts)
        assert any("func add" in t for t in texts)

    def test_page_numbers_sequential(self, tmp_path: Path):
        f = tmp_path / "seq.py"
        f.write_text(
            "def a():\n    pass\n\n\ndef b():\n    pass\n\n\ndef c():\n    pass\n"
        )

        pages = process_code_file(f)

        assert len(pages) == 3
        for i, page in enumerate(pages):
            assert page.page_number == i + 1
            assert page.total_pages == 3

    def test_decorated_function(self, tmp_path: Path):
        f = tmp_path / "deco.py"
        f.write_text(
            "import functools\n\n\n@functools.cache\ndef expensive():\n    return 42\n"
        )

        pages = process_code_file(f)

        # Decorator + function should be one section
        assert len(pages) == 2
        assert "import functools" in pages[0].text
        assert "@functools.cache" in pages[1].text
        assert "def expensive" in pages[1].text


class TestFallbackSplit:
    def test_splits_on_blank_lines(self):
        text = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        sections = _fallback_split(text)
        assert len(sections) == 2

    def test_skips_whitespace_only_sections(self):
        text = "code\n\n   \n\nmore code"
        sections = _fallback_split(text)
        assert len(sections) == 2


class TestTreeSitterEdgeCases:
    def test_returns_none_for_unknown_language(self):
        result = _split_with_treesitter(
            "some code", "nonexistent_language_xyz", "test.xyz"
        )
        assert result is None

    def test_single_function_returns_one_section(self):
        result = _split_with_treesitter(
            "def hello():\n    print('hi')\n", "python", "test.py"
        )
        assert result is not None
        assert len(result) == 1
        assert "def hello" in result[0]


class TestImportsGrouped:
    """Imports and small top-level statements should be grouped."""

    def test_imports_grouped_separately_from_functions(self, tmp_path: Path):
        f = tmp_path / "mod.py"
        f.write_text("import os\nimport sys\n\n\ndef main():\n    print(os.getcwd())\n")

        pages = process_code_file(f)

        assert len(pages) == 2
        assert "import os" in pages[0].text
        assert "import sys" in pages[0].text
        assert "def main" in pages[1].text

    def test_constants_grouped_with_imports(self, tmp_path: Path):
        f = tmp_path / "config.py"
        f.write_text(
            "import os\n\n"
            "BASE_DIR = os.path.dirname(__file__)\n"
            "DEBUG = True\n\n\n"
            "def get_config():\n"
            "    return {}\n"
        )

        pages = process_code_file(f)

        assert len(pages) == 2
        assert "import os" in pages[0].text
        assert "BASE_DIR" in pages[0].text
        assert "def get_config" in pages[1].text
