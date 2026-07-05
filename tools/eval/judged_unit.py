"""JudgedUnit: the stable (document, page) join key shared by runs and qrels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from quarry.results import SearchResult

# Collapse any run of whitespace so a docid is a single TREC-safe token
# (TREC run/qrels lines are whitespace-delimited; a space in a docid corrupts
# the column layout).
_WS = re.compile(r"\s+")

# page_number is a *section ordinal* for code/markdown, 1-indexed by the
# extractor. A value at or below this sentinel means "no stable page" and the
# unit degrades to document granularity (design section 2: null-safe).
_NO_PAGE = 0


@dataclass(frozen=True, slots=True)
class JudgedUnit:
    """A relevance-judged unit of retrieval: a document, optionally a page.

    The key is ``(document_name, page_number)``. ``page_number`` is fixed by
    the extractor *before* chunking, so it is stable across chunking configs —
    unlike ``chunk_index``, which must never be a key. For non-paginated
    formats (code, markdown) the ordinal degrades to document granularity when
    it is null or non-positive. ``docid`` is the single string both the run and
    the qrels join on, so emission and authoring cannot drift.
    """

    document_name: str
    page_number: int | None = None

    @property
    def is_page_level(self) -> bool:
        """Whether this unit carries a usable page ordinal (else document-level)."""
        return self.page_number is not None and self.page_number > _NO_PAGE

    @property
    def docid(self) -> str:
        """Return the whitespace-free TREC docid both run and qrels join on."""
        slug = _WS.sub("_", self.document_name.strip())
        if self.is_page_level:
            return f"{slug}#p{self.page_number}"
        return f"{slug}#doc"

    @classmethod
    def from_result(cls, result: SearchResult, *, page_level: bool) -> Self:
        """Build the unit a search hit collapses to, at the chosen granularity.

        With ``page_level`` the unit keys on the hit's extractor page ordinal;
        otherwise (or when that ordinal is absent) it keys on the document.
        """
        page = result.page_number if page_level else None
        return cls(document_name=result.document_name, page_number=page)

    @classmethod
    def document(cls, document_name: str) -> Self:
        """Build a document-granularity unit (no page ordinal)."""
        return cls(document_name=document_name, page_number=None)
