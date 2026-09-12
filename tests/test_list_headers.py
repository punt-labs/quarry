"""Regression tests: list-command header counts match printed row counts.

The panel summary that consumers see ("N documents", "N registrations") must
equal the number of items the formatter actually rendered, even when a row
wraps across multiple lines because a variable-width column overflowed.

Before the fix the plugin's PostToolUse hook derived the count by
``tail -n +2 | wc -l``, which counts continuation lines as extra rows: a
two-row table with one wrapped row was summarised as "3 documents". The fix
puts an authoritative count on the first line of the formatter output; the
hook just echoes that line as the panel summary.
"""

from __future__ import annotations

import re

from quarry.formatting import (
    format_collections,
    format_databases,
    format_documents,
    format_registrations,
)

_COUNT_LINE = re.compile(r"^▶\s+(\d+)\s+(\w+)")


def _header_count(rendered: str) -> int:
    first = rendered.splitlines()[0]
    match = _COUNT_LINE.match(first)
    assert match, f"first line must be '▶  N noun', got {first!r}"
    return int(match.group(1))


class TestDocumentsHeader:
    def test_count_matches_row_count(self) -> None:
        docs = [
            {
                "document_name": f"doc-{i}.pdf",
                "collection": "col",
                "total_pages": 1,
                "chunk_count": 1,
            }
            for i in range(3)
        ]
        assert _header_count(format_documents(docs)) == 3

    def test_count_survives_wrapped_variable_column(self) -> None:
        # A long document name forces the DOCUMENT column (variable) to wrap
        # across multiple lines. The previous count-by-line-count summary
        # would have reported this as more items than were present.
        long_name = "a" * 200 + ".md"
        docs = [
            {
                "document_name": long_name,
                "collection": "col",
                "total_pages": 1,
                "chunk_count": 1,
            },
            {
                "document_name": "short.md",
                "collection": "col",
                "total_pages": 1,
                "chunk_count": 1,
            },
        ]
        rendered = format_documents(docs)
        # Sanity: the long-name row DID wrap — the raw output must be more
        # than one header + N rows (else this test is not exercising the bug).
        line_count = len(rendered.splitlines())
        expected_if_no_wrap = 1 + 1 + len(docs)  # count-header, col-header, rows
        assert line_count > expected_if_no_wrap, (
            "long name did not wrap; test would not exercise the regression"
        )
        assert _header_count(rendered) == 2

    def test_singular_when_one(self) -> None:
        docs = [
            {
                "document_name": "one.pdf",
                "collection": "col",
                "total_pages": 1,
                "chunk_count": 1,
            }
        ]
        first = format_documents(docs).splitlines()[0]
        assert "1 document" in first
        assert "1 documents" not in first


class TestRegistrationsHeader:
    def test_count_matches_row_count(self) -> None:
        regs = [
            {
                "collection": f"col-{i}",
                "directory": "/short",
                "registered_at": "2026-01-01",
            }
            for i in range(3)
        ]
        assert _header_count(format_registrations(regs)) == 3

    def test_count_survives_wrapped_variable_column(self) -> None:
        long_dir = "/" + "/".join(["nested"] * 40)
        regs = [
            {
                "collection": "a",
                "directory": long_dir,
                "registered_at": "2026-01-01",
            },
            {
                "collection": "b",
                "directory": "/short/b",
                "registered_at": "2026-01-02",
            },
            {
                "collection": "c",
                "directory": "/short/c",
                "registered_at": "2026-01-03",
            },
        ]
        rendered = format_registrations(regs)
        line_count = len(rendered.splitlines())
        expected_if_no_wrap = 1 + 1 + len(regs)
        assert line_count > expected_if_no_wrap, (
            "long directory did not wrap; test would not exercise the regression"
        )
        assert _header_count(rendered) == 3


class TestCollectionsHeader:
    def test_count_matches_row_count(self) -> None:
        cols = [
            {"collection": f"c-{i}", "document_count": 1, "chunk_count": 1}
            for i in range(4)
        ]
        assert _header_count(format_collections(cols)) == 4


class TestDatabasesHeader:
    def test_count_matches_row_count(self) -> None:
        dbs = [{"name": f"db-{i}", "document_count": 10} for i in range(2)]
        assert _header_count(format_databases(dbs, current="db-0")) == 2
