"""Unit tests for Collapser: best-rank-per-page collapse and deterministic order."""

from __future__ import annotations

from quarry.results import SearchResult
from tools.eval.indexing import Collapser


def _chunk(document_name: str, *, page: int, chunk: int) -> SearchResult:
    return SearchResult(
        document_name=document_name,
        collection="eval",
        page_number=page,
        chunk_index=chunk,
        text="prose",
        page_type="section",
        source_format=".md",
        agent_handle="",
        memory_type="",
        summary="",
        distance=0.2,
    )


def test_collapse_keeps_best_rank_per_page() -> None:
    # Page one of a.md appears at list positions zero and two; only the best
    # (earliest) position survives. Page one of b.md sits at position one, page
    # two of a.md at position three. Rows come out ordered by best position, and
    # each score is the reciprocal of one-plus-position, so it strictly decreases.
    results = [
        _chunk("a.md", page=1, chunk=0),
        _chunk("b.md", page=1, chunk=0),
        _chunk("a.md", page=1, chunk=1),
        _chunk("a.md", page=2, chunk=0),
    ]
    rows = Collapser.collapse(results, page_level=True)
    assert [docid for docid, _ in rows] == ["a.md#p1", "b.md#p1", "a.md#p2"]
    assert [score for _, score in rows] == [1.0, 0.5, 0.25]  # 1/(pos+1), pos 0,1,3


def test_collapse_document_level_folds_pages_into_one_unit() -> None:
    # At document granularity every chunk of a.md collapses to one document unit
    # at its best position zero, while b.md keeps position one.
    results = [
        _chunk("a.md", page=1, chunk=0),
        _chunk("b.md", page=1, chunk=0),
        _chunk("a.md", page=2, chunk=0),
    ]
    rows = Collapser.collapse(results, page_level=False)
    assert [docid for docid, _ in rows] == ["a.md#doc", "b.md#doc"]
    assert [score for _, score in rows] == [1.0, 0.5]


def test_collapse_tie_break_is_deterministic_by_position_then_docid() -> None:
    # Positions from enumerate are unique, so the (position, docid) sort key can
    # never actually tie on position — the docid component is the defensive
    # tie-break the determinism contract demands. Pin that the output is a stable
    # sort by ascending best position across repeated calls.
    results = [
        _chunk("c.md", page=1, chunk=0),
        _chunk("a.md", page=1, chunk=0),
        _chunk("b.md", page=1, chunk=0),
    ]
    first = Collapser.collapse(results, page_level=True)
    second = Collapser.collapse(results, page_level=True)
    assert first == second
    assert [docid for docid, _ in first] == ["c.md#p1", "a.md#p1", "b.md#p1"]
    positions = [round(1.0 / score - 1) for _, score in first]
    assert positions == sorted(positions)  # ascending best position
