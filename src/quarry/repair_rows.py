"""Judge each stored row: keep it, or drop it as binary or over the row budget."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self, final

from quarry.content_guard import BinaryContent, UnindexableContentError
from quarry.db.row_budget import RowBudget, RowBudgetError

if TYPE_CHECKING:
    from collections.abc import Collection

Verdict = Literal["keep", "binary", "over_budget"]

_TEXT_COLUMNS = ("text", "page_raw_text")


@final
class RowJudge:
    """Classify rows against the row budget and a set of binary documents.

    A document is binary when any one of its rows sniffs as binary, so every
    row of it is dropped together.  A row over the budget is dropped on its own.
    """

    __slots__ = ("_bad_documents", "_binary", "_budget", "_labels")

    _budget: RowBudget
    _binary: BinaryContent
    _labels: tuple[str, ...]
    _bad_documents: set[tuple[str, str]]

    def __new__(cls, columns: Collection[str]) -> Self:
        self = super().__new__(cls)
        self._budget = RowBudget()
        self._binary = BinaryContent()
        self._labels = tuple(c for c in RowBudget.LABEL_COLUMNS if c in columns)
        self._bad_documents = set()
        return self

    @staticmethod
    def document_of(row: dict[str, object]) -> tuple[str, str]:
        """Return the ``(collection, document_name)`` a row belongs to."""
        return (str(row["collection"]), str(row["document_name"]))

    def note(self, row: dict[str, object]) -> None:
        """Record the row's document as binary when its text sniffs as binary."""
        try:
            self._binary.sniff(str(row["text"]))
        except UnindexableContentError:
            self._bad_documents.add(self.document_of(row))

    def verdict(self, row: dict[str, object]) -> Verdict:
        """Return whether to keep *row*, or why it is dropped."""
        if self.document_of(row) in self._bad_documents:
            return "binary"
        try:
            for column in _TEXT_COLUMNS:
                if column in row:
                    self._budget.require_page(str(row[column]))
            for column in self._labels:
                self._budget.require_label(column, str(row[column]))
        except RowBudgetError:
            return "over_budget"
        return "keep"
