"""Tests for the MCP output formatting module."""

from __future__ import annotations

from quarry.api import InsightsResponse
from quarry.formatting import (
    format_collections,
    format_databases,
    format_documents,
    format_registrations,
    format_search_results,
)
from quarry.reports import InsightsReport
from quarry.table_layout import ColumnSpec, TableLayout


class TestHelpers:
    def test_visible_width_plain(self) -> None:
        assert TableLayout.visible_width("hello") == 5

    def test_visible_width_ansi(self) -> None:
        assert TableLayout.visible_width("\x1b[31mred\x1b[0m") == 3

    def test_truncate_short(self) -> None:
        assert TableLayout.truncate("hello world", 20) == "hello world"

    def test_truncate_long(self) -> None:
        result = TableLayout.truncate("a" * 200, 50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_truncate_collapses_whitespace(self) -> None:
        assert TableLayout.truncate("hello   \n  world") == "hello world"


class TestFormatTable:
    def test_basic_table(self) -> None:
        specs = [
            ColumnSpec("NAME", 4),
            ColumnSpec("COUNT", 5, align="right"),
        ]
        rows = [["alpha", "10"], ["beta", "200"]]
        result = TableLayout(specs).render(rows)
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
        result = TableLayout(specs).render(rows)
        lines = result.split("\n")
        assert len(lines) > 2  # Header + at least 2 wrapped lines

    def test_empty_rows(self) -> None:
        specs = [ColumnSpec("NAME", 4)]
        result = TableLayout(specs).render([])
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
            {"name": "default", "document_count": 5},
            {"name": "work", "document_count": 10},
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


_EMPTY_INSIGHTS = InsightsResponse(
    telemetry_enabled=True,
    total_queries=0,
    empty_result_rate=0.0,
    p50_latency_ms=0.0,
    p95_latency_ms=0.0,
    top_empty_queries=[],
    per_collection_hits=[],
    per_agent_recall=[],
    memory_queries=0,
    knowledge_queries=0,
    hit_decay_bands=[],
)

_POPULATED_INSIGHTS = InsightsResponse(
    telemetry_enabled=True,
    total_queries=10,
    empty_result_rate=0.2,
    p50_latency_ms=12.3,
    p95_latency_ms=45.6,
    top_empty_queries=[{"query_scrubbed": "nada", "count": 2}],
    per_collection_hits=[{"collection": "default", "hit_count": 5}],
    per_agent_recall=[{"agent_handle": "rmh", "query_count": 3}],
    memory_queries=3,
    knowledge_queries=7,
    hit_decay_bands=[{"band": "0-7d", "hit_count": 4}],
)


class TestFormatInsights:
    """Shared by the CLI (``quarry insights``) and the MCP ``insights`` tool."""

    def test_empty_store_renders_without_breakdown_sections(self) -> None:
        text = InsightsReport(_EMPTY_INSIGHTS).render()
        assert "Total queries:    0" in text
        assert "Top empty queries:" not in text
        assert "Hits by collection:" not in text

    def test_telemetry_disabled_renders_as_disabled(self) -> None:
        disabled = _EMPTY_INSIGHTS.model_copy(update={"telemetry_enabled": False})
        text = InsightsReport(disabled).render()
        assert "disabled" in text

    def test_populated_store_renders_every_breakdown(self) -> None:
        text = InsightsReport(_POPULATED_INSIGHTS).render()
        assert "Top empty queries:" in text
        assert "nada: 2" in text
        assert "Hits by collection:" in text
        assert "default: 5" in text
        assert "Recall by agent:" in text
        assert "rmh: 3" in text
        assert "Hit decay bands:" in text
        assert "0-7d: 4" in text

    def test_latency_and_rate_are_formatted(self) -> None:
        text = InsightsReport(_POPULATED_INSIGHTS).render()
        assert "20.0%" in text
        assert "12.3ms" in text
        assert "45.6ms" in text
