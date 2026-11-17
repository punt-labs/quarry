"""Judge each stored row: keep it, or drop it as binary or over the row budget."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Self, final

from quarry.content_guard import BinaryContent, UnindexableContentError
from quarry.db.row_budget import RowBudget, RowBudgetError

if TYPE_CHECKING:
    from collections.abc import Collection

Verdict = Literal["keep", "binary", "over_budget"]

_OPENING_CHARS = 512
_PAD_FACTOR = 64
_LEGACY_PAGE_COLUMN = "page_raw_text"
# The same class of characters BinaryContent counts as non-text.
_NONTEXT = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\ufffd]")


@final
@dataclass(slots=True)
class _DocumentTally:
    """What the document-level binary verdict needs, held in constant space."""

    opening: str = ""
    nontext: int = 0
    total: int = 0


@final
class RowJudge:
    """Classify rows against the row budget and a set of binary documents.

    A document is binary by the bar ingest admitted it with: it opens with a
    binary signature, or its non-text share over all its chunks exceeds the
    limit.  One odd chunk among clean ones does not condemn it.  A row over the
    budget (a chunk over the chunk limit, a page over the page limit, a label
    over the label limit) is dropped on its own.
    """

    __slots__ = ("_binary", "_budget", "_labels", "_tallies", "_verdicts")

    _budget: RowBudget
    _binary: BinaryContent
    _labels: tuple[str, ...]
    _tallies: dict[tuple[str, str], _DocumentTally]
    _verdicts: dict[tuple[str, str], bool] | None

    def __new__(cls, columns: Collection[str]) -> Self:
        self = super().__new__(cls)
        self._budget = RowBudget()
        self._binary = BinaryContent()
        self._labels = tuple(c for c in RowBudget.LABEL_COLUMNS if c in columns)
        self._tallies = {}
        self._verdicts = None
        return self

    @staticmethod
    def document_of(row: dict[str, object]) -> tuple[str, str]:
        """Return the ``(collection, document_name)`` a row belongs to."""
        return (str(row["collection"]), str(row["document_name"]))

    def note(self, row: dict[str, object]) -> None:
        """Add the row's chunk text to its document's tally."""
        text = str(row["text"])
        tally = self._tallies.setdefault(self.document_of(row), _DocumentTally())
        # Rows come back in storage order, not chunk order, so only the row that
        # is chunk 0 may speak for the document's opening.
        if int(str(row["chunk_index"])) == 0:
            tally.opening = text[:_OPENING_CHARS]
        tally.nontext += len(_NONTEXT.findall(text))
        tally.total += len(text)

    def verdict(self, row: dict[str, object]) -> Verdict:
        """Return whether to keep *row*, or why it is dropped."""
        if self._is_binary(self.document_of(row)):
            return "binary"
        try:
            self._budget.require_chunk(str(row["text"]))
            if _LEGACY_PAGE_COLUMN in row:
                self._budget.require_page(str(row[_LEGACY_PAGE_COLUMN]))
            for column in self._labels:
                self._budget.require_label(column, str(row[column]))
        except RowBudgetError:
            return "over_budget"
        return "keep"

    def _is_binary(self, document: tuple[str, str]) -> bool:
        if self._verdicts is None:
            self._verdicts = {k: self._judge(t) for k, t in self._tallies.items()}
        return self._verdicts.get(document, False)

    def _judge(self, tally: _DocumentTally) -> bool:
        if tally.total == 0:
            return False
        if tally.nontext > self._binary.max_nontext_ratio * tally.total:
            return True
        # Only the opening's signature is wanted here; the ratio was judged over
        # every chunk above, so pad the opening with clean text until it cannot
        # trip the ratio on its own.
        padded = tally.opening + " " * (_OPENING_CHARS * _PAD_FACTOR)
        try:
            self._binary.sniff(padded)
        except UnindexableContentError:
            return True
        return False
