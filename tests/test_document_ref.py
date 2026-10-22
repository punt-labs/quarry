"""Tests for quarry.db.document_ref: the scoped document selector."""

from __future__ import annotations

import pytest

from quarry.db.document_ref import DocumentRef


class TestInvariants:
    def test_rejects_empty_document_name(self) -> None:
        with pytest.raises(ValueError, match="document_name must be non-empty"):
            DocumentRef("", "col")

    def test_rejects_negative_watermark(self) -> None:
        with pytest.raises(ValueError, match="min_chunk_index must be >= 0"):
            DocumentRef("doc.txt", "col", -1)

    def test_zero_watermark_is_a_valid_whole_document_ref(self) -> None:
        # min 0 is legal for a whole-document selector (delete_document uses it);
        # only delete_document_tail additionally requires a positive watermark.
        ref = DocumentRef("doc.txt", "col")
        assert ref.min_chunk_index == 0
        assert "chunk_index" not in ref.predicate()


class TestPredicate:
    def test_whole_document(self) -> None:
        assert DocumentRef("a.txt").predicate() == "document_name = 'a.txt'"

    def test_collection_scopes_the_predicate(self) -> None:
        assert DocumentRef("a.txt", "col").predicate() == (
            "document_name = 'a.txt' AND collection = 'col'"
        )

    def test_watermark_selects_the_resume_tail(self) -> None:
        assert DocumentRef("a.txt", "col", 5).predicate().endswith("chunk_index >= 5")

    def test_quotes_are_escaped(self) -> None:
        assert DocumentRef("it's.txt").predicate() == "document_name = 'it''s.txt'"
