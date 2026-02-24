"""Tests for the MCP output formatting module."""

from __future__ import annotations

from quarry.formatting import (
    ColumnSpec,
    _fmt_size,
    format_collections,
    format_databases,
    format_delete_summary,
    format_deregister_summary,
    format_documents,
    format_ingest_summary,
    format_register_summary,
    format_registrations,
    format_search_results,
    format_sitemap_summary,
    format_status,
    format_switch_summary,
    format_sync_summary,
    format_table,
    truncate,
    visible_width,
)


class TestHelpers:
    def test_visible_width_plain(self) -> None:
        assert visible_width("hello") == 5

    def test_visible_width_ansi(self) -> None:
        assert visible_width("\x1b[31mred\x1b[0m") == 3

    def test_truncate_short(self) -> None:
        assert truncate("hello world", 20) == "hello world"

    def test_truncate_long(self) -> None:
        result = truncate("a" * 200, 50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_truncate_collapses_whitespace(self) -> None:
        assert truncate("hello   \n  world") == "hello world"

    def test_fmt_size_bytes(self) -> None:
        assert _fmt_size(512) == "512 B"

    def test_fmt_size_kb(self) -> None:
        assert _fmt_size(2048) == "2.0 KB"

    def test_fmt_size_mb(self) -> None:
        assert _fmt_size(5 * 1024 * 1024) == "5.0 MB"

    def test_fmt_size_gb(self) -> None:
        assert _fmt_size(2 * 1024 * 1024 * 1024) == "2.0 GB"


class TestFormatTable:
    def test_basic_table(self) -> None:
        specs = [
            ColumnSpec("NAME", 4),
            ColumnSpec("COUNT", 5, align="right"),
        ]
        rows = [["alpha", "10"], ["beta", "200"]]
        result = format_table(specs, rows)
        assert "\u25b6" in result  # Header prefix
        assert "NAME" in result
        assert "COUNT" in result
        assert "alpha" in result
        assert "200" in result

    def test_variable_column_wraps(self) -> None:
        specs = [
            ColumnSpec("ID", 2),
            ColumnSpec("TEXT", 10, fixed=False),
        ]
        long_text = "word " * 30
        rows = [["1", long_text.strip()]]
        result = format_table(specs, rows)
        lines = result.split("\n")
        assert len(lines) > 2  # Header + at least 2 wrapped lines

    def test_empty_rows(self) -> None:
        specs = [ColumnSpec("NAME", 4)]
        result = format_table(specs, [])
        assert "NAME" in result


class TestSearchResults:
    def test_empty_results(self) -> None:
        result = format_search_results("test query", [])
        assert "No results" in result
        assert "test query" in result

    def test_single_result(self) -> None:
        results = [
            {
                "document_name": "report.pdf",
                "page_number": 3,
                "similarity": 0.85,
                "text": "quarterly revenue grew 15%",
            }
        ]
        result = format_search_results("revenue", results)
        assert "1 result" in result
        assert "revenue" in result
        assert "report.pdf" in result
        assert "p3" in result
        assert "0.85" in result
        assert "quarterly revenue grew" in result

    def test_multiple_results(self) -> None:
        results = [
            {
                "document_name": "a.pdf",
                "page_number": 1,
                "similarity": 0.9,
                "text": "first",
            },
            {
                "document_name": "b.pdf",
                "page_number": 2,
                "similarity": 0.7,
                "text": "second",
            },
        ]
        result = format_search_results("query", results)
        assert "2 results" in result
        assert "1." in result
        assert "2." in result

    def test_long_text_truncated(self) -> None:
        results = [
            {
                "document_name": "doc.pdf",
                "page_number": 1,
                "similarity": 0.5,
                "text": "word " * 100,
            }
        ]
        result = format_search_results("q", results)
        assert "..." in result


class TestDocuments:
    def test_empty(self) -> None:
        assert format_documents([]) == "No documents"

    def test_with_docs(self) -> None:
        docs = [
            {
                "document_name": "report.pdf",
                "collection": "finance",
                "total_pages": 10,
                "chunk_count": 42,
            }
        ]
        result = format_documents(docs)
        assert "DOCUMENT" in result
        assert "report.pdf" in result
        assert "finance" in result
        assert "42" in result


class TestCollections:
    def test_empty(self) -> None:
        assert format_collections([]) == "No collections"

    def test_with_collections(self) -> None:
        cols = [
            {"collection": "math", "document_count": 5, "chunk_count": 100},
        ]
        result = format_collections(cols)
        assert "COLLECTION" in result
        assert "math" in result
        assert "100" in result


class TestDatabases:
    def test_empty(self) -> None:
        assert format_databases([]) == "No databases"

    def test_marks_current(self) -> None:
        dbs = [
            {"name": "default", "document_count": 5, "size_bytes": 1024},
            {"name": "work", "document_count": 10, "size_bytes": 2048},
        ]
        result = format_databases(dbs, current="work")
        assert "* work" in result
        # 'default' should NOT have the * prefix
        lines = result.split("\n")
        default_line = next(line for line in lines if "default" in line)
        assert "* default" not in default_line


class TestRegistrations:
    def test_empty(self) -> None:
        assert format_registrations([]) == "No registered directories"

    def test_with_registrations(self) -> None:
        regs = [
            {
                "directory": "/home/user/docs",
                "collection": "docs",
                "registered_at": "2025-02-20T10:00:00",
            }
        ]
        result = format_registrations(regs)
        assert "COLLECTION" in result
        assert "docs" in result
        assert "2025-02-20" in result


class TestStatus:
    def test_includes_all_fields(self) -> None:
        info = {
            "document_count": 45,
            "collection_count": 3,
            "chunk_count": 1234,
            "registered_directories": 2,
            "database_path": "/home/user/.quarry/lancedb",
            "database_size_bytes": 12582912,
            "embedding_model": "arctic-embed",
        }
        result = format_status(info)
        assert "quarry status" in result
        assert "45" in result
        assert "1,234" in result
        assert "/home/user/.quarry/lancedb" in result
        assert "12.0 MB" in result
        assert "arctic-embed" in result


class TestActionSummaries:
    def test_ingest_summary(self) -> None:
        result = format_ingest_summary(
            {"document_name": "report.pdf", "chunks": 42, "collection": "finance"}
        )
        assert "report.pdf" in result
        assert "42 chunks" in result
        assert "finance" in result
        assert "\u25b6" in result

    def test_sitemap_summary(self) -> None:
        result = format_sitemap_summary(
            {
                "ingested": 45,
                "skipped": 3,
                "failed": 1,
                "total_discovered": 628,
                "after_filter": 50,
                "collection": "docs.example.com",
            }
        )
        assert "45 ingested" in result
        assert "3 skipped" in result
        assert "1 failed" in result
        assert "628 discovered" in result

    def test_sitemap_summary_no_skipped_or_failed(self) -> None:
        result = format_sitemap_summary(
            {
                "ingested": 10,
                "skipped": 0,
                "failed": 0,
                "total_discovered": 10,
                "after_filter": 10,
                "collection": "docs",
            }
        )
        assert "skipped" not in result
        assert "failed" not in result

    def test_sync_summary(self) -> None:
        result = format_sync_summary(
            {
                "collections_synced": 2,
                "results": {
                    "a": {"ingested": 3, "deleted": 1, "skipped": 5},
                    "b": {"ingested": 0, "deleted": 0, "skipped": 2},
                },
            }
        )
        assert "Synced 2 collections" in result
        assert "3 ingested" in result
        assert "1 deleted" in result
        assert "7 skipped" in result

    def test_sync_summary_empty(self) -> None:
        result = format_sync_summary({"collections_synced": 0, "results": {}})
        assert "No collections to sync" in result

    def test_delete_summary_singular(self) -> None:
        result = format_delete_summary("document", "report.pdf", 1)
        assert "1 chunk)" in result
        assert "chunks" not in result

    def test_delete_summary_plural(self) -> None:
        result = format_delete_summary("collection", "math", 50)
        assert "50 chunks" in result

    def test_register_summary(self) -> None:
        result = format_register_summary("/home/user/docs", "docs")
        assert "/home/user/docs" in result
        assert "docs" in result
        assert "\u2192" in result

    def test_deregister_summary_with_data(self) -> None:
        result = format_deregister_summary("math", 5, data_deleted=True)
        assert "math" in result
        assert "5 docs removed" in result

    def test_deregister_summary_keep_data(self) -> None:
        result = format_deregister_summary("math", 5, data_deleted=False)
        assert "math" in result
        assert "removed" not in result

    def test_switch_summary(self) -> None:
        result = format_switch_summary("default", "work", "/path/to/lancedb")
        assert "default" in result
        assert "work" in result
        assert "/path/to/lancedb" in result
