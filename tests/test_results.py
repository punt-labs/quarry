"""Tests for result structures and SearchFilter predicate builder."""

from __future__ import annotations

import dataclasses

import pytest

from quarry.results import SearchFilter, SearchResult


class TestSearchResultSimilarity:
    def test_normal_distance_yields_cosine(self):
        assert SearchResult.from_row({"_distance": 0.1}).similarity == 0.9

    def test_zero_distance_is_perfect(self):
        assert SearchResult.from_row({"_distance": 0.0}).similarity == 1.0

    def test_missing_distance_sinks_to_worst_case(self):
        # A row lacking _distance defaults to distance 2.0 => similarity -1,
        # never a fake perfect 1.0 (quarry-gcnf).
        assert SearchResult.from_row({}).similarity == -1.0

    def test_rounding_to_four_places(self):
        assert SearchResult.from_row({"_distance": 0.123456}).similarity == 0.8765

    def test_string_valued_distance_is_coerced(self):
        assert SearchResult.from_row({"_distance": "0.25"}).similarity == 0.75


class TestSearchResultFromRow:
    def test_missing_string_fields_default_empty(self):
        r = SearchResult.from_row({"_distance": 0.1})
        assert r.document_name == ""
        assert r.collection == ""
        assert r.text == ""
        assert r.agent_handle == ""
        assert r.memory_type == ""
        assert r.summary == ""

    def test_missing_counts_default_zero(self):
        r = SearchResult.from_row({"_distance": 0.1})
        assert r.page_number == 0
        assert r.chunk_index == 0

    def test_populated_row_round_trips_to_dict(self):
        row = {
            "document_name": "doc.md",
            "collection": "default",
            "page_number": 3,
            "chunk_index": 7,
            "text": "hello",
            "page_type": "body",
            "source_format": ".md",
            "agent_handle": "rmh",
            "memory_type": "fact",
            "summary": "a summary",
            "_distance": 0.1,
        }
        d = SearchResult.from_row(row).to_dict()
        assert d == {
            "document_name": "doc.md",
            "collection": "default",
            "page_number": 3,
            "chunk_index": 7,
            "text": "hello",
            "page_type": "body",
            "source_format": ".md",
            "agent_handle": "rmh",
            "memory_type": "fact",
            "summary": "a summary",
            "similarity": 0.9,
        }

    def test_frozen(self):
        r = SearchResult.from_row({"_distance": 0.1})
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.distance = 0.5  # type: ignore[misc]

    def test_float_like_string_count_is_coerced(self):
        r = SearchResult.from_row({"page_number": "3.0", "_distance": 0.1})
        assert r.page_number == 3


class TestSearchResultDistanceInvariant:
    def test_negative_distance_raises(self):
        with pytest.raises(ValueError, match="outside cosine range"):
            SearchResult.from_row({"_distance": -0.5})

    def test_distance_above_two_raises(self):
        with pytest.raises(ValueError, match="outside cosine range"):
            SearchResult.from_row({"_distance": 2.5})

    def test_worst_case_distance_is_accepted(self):
        # distance 2.0 is the valid worst case -> similarity -1, must not raise.
        r = SearchResult.from_row({"_distance": 2.0})
        assert r.similarity == -1.0

    def test_float_overshoot_within_tolerance_is_accepted(self):
        # A unit-vector cosine can float-overshoot to ~-1.0000001.
        r = SearchResult.from_row({"_distance": 2.0000001})
        assert r.distance == pytest.approx(2.0000001)

    def test_normal_distance_is_accepted(self):
        assert SearchResult.from_row({"_distance": 0.3}).similarity == 0.7


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
