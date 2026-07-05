"""Unit tests for JudgedUnit: the docid join key and its null-safe degrade."""

from __future__ import annotations

from quarry.results import SearchResult
from tools.eval.judged_unit import JudgedUnit


def _result(document_name: str, page_number: int) -> SearchResult:
    return SearchResult(
        document_name=document_name,
        collection="eval",
        page_number=page_number,
        chunk_index=0,
        text="body",
        page_type="section",
        source_format=".md",
        agent_handle="",
        memory_type="",
        summary="",
        distance=0.1,
    )


def test_page_level_docid_uses_ordinal() -> None:
    unit = JudgedUnit("predicate-logic-in-z.tex", 2)
    assert unit.is_page_level
    assert unit.docid == "predicate-logic-in-z.tex#p2"


def test_none_page_degrades_to_document_level() -> None:
    unit = JudgedUnit("design.md", None)
    assert not unit.is_page_level
    assert unit.docid == "design.md#doc"


def test_zero_or_negative_page_degrades_to_document_level() -> None:
    assert JudgedUnit("a.md", 0).docid == "a.md#doc"
    assert JudgedUnit("a.md", -1).docid == "a.md#doc"


def test_whitespace_in_name_is_collapsed_to_token() -> None:
    # docids sit in whitespace-delimited TREC columns; a space would corrupt them.
    assert JudgedUnit("my notes v2.md", 3).docid == "my_notes_v2.md#p3"


def test_from_result_page_level_reads_the_page_ordinal() -> None:
    unit = JudgedUnit.from_result(_result("x.md", 4), page_level=True)
    assert unit.docid == "x.md#p4"


def test_from_result_document_level_drops_the_page() -> None:
    unit = JudgedUnit.from_result(_result("x.md", 4), page_level=False)
    assert unit.docid == "x.md#doc"


def test_document_factory_is_document_level() -> None:
    assert JudgedUnit.document("x.md").docid == "x.md#doc"


def test_equal_units_share_a_docid() -> None:
    assert JudgedUnit("x.md", 1) == JudgedUnit("x.md", 1)
    assert JudgedUnit("x.md", 1).docid == JudgedUnit("x.md", 1).docid
