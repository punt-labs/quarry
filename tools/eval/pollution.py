"""Metadata-pollution classifier: a reported-only retrieval diagnostic.

It labels a *chunk* as structural metadata (changelog / TOC / frontmatter /
heading stub) rather than substantive content. metadata-pollution@10 is the
fraction of a query's top-10 chunks so labelled. Per the design it is a
guardrail diagnostic: reported, never gated, never optimized. The rules are
heuristic over the chunk text alone (``result.page_type`` is not inspected) and
are meant to be refined against the fixture, not treated as ground truth.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Sequence

    from quarry.results import SearchResult

# A changelog version header (`## [2.1.0] - ...`, `## 2.0.0`) or a keep-a-
# changelog section (`### Added`, `### Fixed`, ...).
_CHANGELOG_HEADER = re.compile(r"^#{1,4}\s*\[?\d+\.\d+", re.MULTILINE)
_CHANGELOG_SECTION = re.compile(
    r"^#{2,4}\s+(Added|Changed|Fixed|Removed|Deprecated|Security)\b",
    re.MULTILINE | re.IGNORECASE,
)
# A markdown link list item, the shape a table of contents is built from.
_TOC_ITEM = re.compile(r"^\s*[-*]\s*\[.+\]\(.+\)\s*$")
# A `key: value` metadata line, the shape of YAML/TOML frontmatter.
_FRONTMATTER_LINE = re.compile(r"^[A-Za-z][\w .-]*:\s+\S")
_HEADING_LINE = re.compile(r"^\s*#{1,6}\s+\S")
_WORD = re.compile(r"[A-Za-z]{2,}")

_TITLE_TERMS = ("changelog", "table of contents", "revision history")


class MetadataPollutionClassifier:
    """Classify chunks as structural metadata vs substantive content.

    ``min_prose_words`` is the floor of real prose words below which a chunk
    that is mostly headings or list items is treated as a structural stub.
    """

    __slots__ = ("_min_prose_words",)

    _min_prose_words: int

    def __new__(cls, min_prose_words: int = 12) -> Self:
        self = super().__new__(cls)
        self._min_prose_words = min_prose_words
        return self

    def is_structural(self, result: SearchResult) -> bool:
        """Return whether this chunk reads as structural metadata."""
        text = result.text
        if self._has_changelog_shape(text) or self._has_title_term(text):
            return True
        if self._is_toc(text):
            return True
        return self._is_frontmatter(text) or self._is_thin_heading_stub(text)

    def pollution_ratio(self, results: Sequence[SearchResult], k: int) -> float:
        """Return the fraction of the top-*k* chunks classified structural."""
        top = list(results[:k])
        if not top:
            return 0.0
        hits = sum(1 for r in top if self.is_structural(r))
        return hits / len(top)

    @staticmethod
    def _has_changelog_shape(text: str) -> bool:
        return bool(_CHANGELOG_HEADER.search(text) or _CHANGELOG_SECTION.search(text))

    @staticmethod
    def _has_title_term(text: str) -> bool:
        head = text[:120].lower()
        return any(term in head for term in _TITLE_TERMS)

    @staticmethod
    def _content_lines(text: str) -> list[str]:
        return [line for line in text.splitlines() if line.strip()]

    def _is_toc(self, text: str) -> bool:
        lines = self._content_lines(text)
        if len(lines) < 3:
            return False
        toc = sum(1 for line in lines if _TOC_ITEM.match(line))
        return toc >= max(3, len(lines) // 2)

    def _is_frontmatter(self, text: str) -> bool:
        stripped = text.lstrip()
        if stripped.startswith("---"):
            return True
        lines = self._content_lines(text)
        if len(lines) < 2:
            return False
        kv = sum(1 for line in lines if _FRONTMATTER_LINE.match(line))
        return kv >= max(2, (len(lines) * 2) // 3)

    def _is_thin_heading_stub(self, text: str) -> bool:
        lines = self._content_lines(text)
        if not lines:
            return True
        has_heading = any(_HEADING_LINE.match(line) for line in lines)
        prose_words = len(_WORD.findall(text))
        return has_heading and prose_words < self._min_prose_words
