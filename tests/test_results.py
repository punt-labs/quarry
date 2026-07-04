"""Tests for result structures and SearchFilter predicate builder."""

from __future__ import annotations

import dataclasses
from typing import cast

import pytest

from quarry.results import SearchFilter, SearchResult, result_similarity


class TestResultSimilarity:
    def test_normal_distance_yields_cosine(self):
        row = cast("SearchResult", {"_distance": 0.1})
        assert result_similarity(row) == 0.9

    def test_zero_distance_is_perfect(self):
        row = cast("SearchResult", {"_distance": 0.0})
        assert result_similarity(row) == 1.0

    def test_missing_distance_sinks_to_worst_case(self):
        # A row lacking _distance defaults to distance 2.0 => similarity -1,
        # never a fake perfect 1.0 (quarry-gcnf).
        row = cast("SearchResult", {})
        assert result_similarity(row) == -1.0

    def test_rounding_to_four_places(self):
        row = cast("SearchResult", {"_distance": 0.123456})
        assert result_similarity(row) == 0.8765

    def test_string_valued_distance_is_coerced(self):
        row = cast("SearchResult", {"_distance": "0.25"})
        assert result_similarity(row) == 0.75


class TestSearchFilterConstruction:
    def test_defaults(self):
        sf = SearchFilter()
        assert sf.collection is None
        assert sf.document is None
        assert sf.page_type is None
        assert sf.source_format is None
        assert sf.agent_handle is None
        assert sf.memory_type is None

    def test_frozen(self):
        sf = SearchFilter()
        with pytest.raises(dataclasses.FrozenInstanceError):
            sf.collection = "x"  # type: ignore[misc]


class TestSearchFilterToPredicate:
    def test_empty_returns_none(self):
        assert SearchFilter().to_predicate() is None

    def test_single_collection(self):
        sf = SearchFilter(collection="docs")
        assert sf.to_predicate() == "collection = 'docs'"

    def test_single_document(self):
        sf = SearchFilter(document="readme.md")
        assert sf.to_predicate() == "document_name = 'readme.md'"

    def test_single_page_type(self):
        sf = SearchFilter(page_type="code")
        assert sf.to_predicate() == "page_type = 'code'"

    def test_single_source_format(self):
        sf = SearchFilter(source_format=".py")
        assert sf.to_predicate() == "source_format = '.py'"

    def test_single_agent_handle(self):
        sf = SearchFilter(agent_handle="rmh")
        assert sf.to_predicate() == "agent_handle = 'rmh'"

    def test_single_memory_type(self):
        sf = SearchFilter(memory_type="fact")
        assert sf.to_predicate() == "memory_type = 'fact'"

    def test_multiple_filters(self):
        sf = SearchFilter(collection="docs", agent_handle="rmh")
        pred = sf.to_predicate()
        assert pred == "collection = 'docs' AND agent_handle = 'rmh'"

    def test_all_filters(self):
        sf = SearchFilter(
            collection="docs",
            document="readme.md",
            page_type="text",
            source_format=".md",
            agent_handle="rmh",
            memory_type="fact",
        )
        pred = sf.to_predicate()
        assert pred is not None
        assert "collection = 'docs'" in pred
        assert "document_name = 'readme.md'" in pred
        assert "page_type = 'text'" in pred
        assert "source_format = '.md'" in pred
        assert "agent_handle = 'rmh'" in pred
        assert "memory_type = 'fact'" in pred
        assert pred.count(" AND ") == 5

    def test_sql_escaping(self):
        sf = SearchFilter(collection="it's")
        assert sf.to_predicate() == "collection = 'it''s'"

    def test_none_values_excluded(self):
        sf = SearchFilter(collection="docs", document=None)
        pred = sf.to_predicate()
        assert pred == "collection = 'docs'"
        assert "document_name" not in pred

    def test_empty_string_excluded(self):
        sf = SearchFilter(collection="docs", document="")
        pred = sf.to_predicate()
        assert pred == "collection = 'docs'"
