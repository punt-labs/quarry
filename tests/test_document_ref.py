"""Tests for quarry.db.document_ref: the scoped collection and document selectors."""

from __future__ import annotations

import pytest

from quarry.db.document_ref import CollectionRef, DocumentRef


class TestCollectionRef:
    def test_clause_selects_the_collection(self) -> None:
        assert CollectionRef("col").clause() == "collection = 'col'"

    def test_quotes_are_escaped(self) -> None:
        assert CollectionRef("it's").clause() == "collection = 'it''s'"

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="name must be non-empty"):
            CollectionRef("")

    @pytest.mark.parametrize("name", [None, ""])
    def test_optional_normalizes_absence_to_none(self, name: str | None) -> None:
        assert CollectionRef.optional(name) is None

    def test_optional_wraps_a_real_name(self) -> None:
        assert CollectionRef.optional("col") == CollectionRef("col")


class TestInvariants:
    def test_rejects_empty_document_name(self) -> None:
        with pytest.raises(ValueError, match="document_name must be non-empty"):
            DocumentRef("")

    def test_rejects_negative_watermark(self) -> None:
        with pytest.raises(ValueError, match="min_chunk_index must be >= 0"):
            DocumentRef("doc.txt", CollectionRef("col"), -1)

    def test_zero_watermark_is_a_valid_whole_document_ref(self) -> None:
        # min 0 is legal for a whole-document selector (delete_document uses it);
        # only a tail additionally requires a positive watermark.
        ref = DocumentRef("doc.txt", CollectionRef("col"))
        assert ref.min_chunk_index == 0
        assert "chunk_index" not in ref.predicate()

    def test_a_tail_needs_a_positive_watermark(self) -> None:
        with pytest.raises(ValueError, match="positive watermark"):
            DocumentRef.tail("doc.txt", None, 0)

    def test_a_negative_watermark_is_refused_by_the_ref_itself(self) -> None:
        with pytest.raises(ValueError, match=">= 0"):
            DocumentRef.tail("doc.txt", None, -1)

    def test_require_tail_refuses_a_whole_document_ref(self) -> None:
        with pytest.raises(ValueError, match="positive watermark"):
            DocumentRef("doc.txt", CollectionRef("col")).require_tail()

    def test_require_tail_returns_a_tail_ref_unchanged(self) -> None:
        ref = DocumentRef.tail("doc.txt", "col", 3)
        assert ref.require_tail() is ref

    def test_a_tail_normalizes_an_empty_collection_to_unscoped(self) -> None:
        assert DocumentRef.tail("doc.txt", "", 5).collection is None

    def test_a_tail_carries_its_watermark(self) -> None:
        ref = DocumentRef.tail("doc.txt", "col", 5)
        assert ref.min_chunk_index == 5


class TestPredicate:
    def test_whole_document(self) -> None:
        assert DocumentRef("a.txt").predicate() == "document_name = 'a.txt'"

    def test_collection_scopes_the_predicate(self) -> None:
        assert DocumentRef("a.txt", CollectionRef("col")).predicate() == (
            "document_name = 'a.txt' AND collection = 'col'"
        )

    def test_watermark_selects_the_resume_tail(self) -> None:
        ref = DocumentRef.tail("a.txt", "col", 5)
        assert ref.predicate().endswith("chunk_index >= 5")

    def test_quotes_are_escaped_in_both_names(self) -> None:
        ref = DocumentRef("it's.txt", CollectionRef("o'brien"))
        assert ref.predicate() == (
            "document_name = 'it''s.txt' AND collection = 'o''brien'"
        )
