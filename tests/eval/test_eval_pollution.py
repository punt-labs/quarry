"""Unit tests for the metadata-pollution classifier (a reported-only diagnostic)."""

from __future__ import annotations

from quarry.results import SearchResult
from tools.eval.pollution import MetadataPollutionClassifier


def _chunk(text: str, *, page_type: str = "section") -> SearchResult:
    return SearchResult(
        document_name="doc.md",
        collection="eval",
        page_number=1,
        chunk_index=0,
        text=text,
        page_type=page_type,
        source_format=".md",
        agent_handle="",
        memory_type="",
        summary="",
        distance=0.2,
    )


_PROSE = (
    "Photosynthesis converts light energy into chemical energy stored in "
    "glucose, releasing oxygen as a byproduct of the light-dependent stage."
)


def test_substantive_prose_is_not_structural() -> None:
    assert not MetadataPollutionClassifier().is_structural(_chunk(_PROSE))


def test_changelog_version_header_is_structural() -> None:
    text = "## [2.1.0] - 2026-03-02\n\n### Fixed\n\n- Corrected a typo."
    assert MetadataPollutionClassifier().is_structural(_chunk(text))


def test_table_of_contents_is_structural() -> None:
    text = (
        "- [Intro](#intro)\n- [Predicate Logic](#pred)\n"
        "- [Schemas](#schemas)\n- [Sequences](#seq)"
    )
    assert MetadataPollutionClassifier().is_structural(_chunk(text))


def test_frontmatter_block_is_structural() -> None:
    text = "title: Predicate Logic\nauthor: Course\ndate: 2026-01-04\nstatus: draft"
    assert MetadataPollutionClassifier().is_structural(_chunk(text))


def test_thin_heading_stub_is_structural() -> None:
    assert MetadataPollutionClassifier().is_structural(_chunk("## Schemas and State"))


def test_pollution_ratio_counts_structural_fraction_of_top_k() -> None:
    results = [
        _chunk("## [1.0.0] - 2026-01-04"),
        _chunk(_PROSE),
        _chunk(_PROSE),
        _chunk(_PROSE),
    ]
    ratio = MetadataPollutionClassifier().pollution_ratio(results, 4)
    assert ratio == 0.25


def test_pollution_ratio_is_zero_for_no_results() -> None:
    assert MetadataPollutionClassifier().pollution_ratio([], 10) == 0.0
