"""The byte budget one stored row may take, and the invariant that bounds a batch.

Arrow ``Utf8`` offsets are 32-bit, so a decode batch whose string column totals
more than 2**31 - 1 bytes fails with ``Offset overflow`` (the failure that made
``table.optimize()`` raise on a PDF stored as 831 rows of 4.5 MB each).  LanceDB's
default scan batch is 1,024 rows, so a row's worst case is bounded here and the
arithmetic is checked on construction rather than left in a comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import final


class RowBudgetError(ValueError):
    """Raised when a row's text exceeds the budget; the text is never truncated."""


@final
@dataclass(frozen=True, slots=True)
class RowBudget:
    """Cap a stored page and a stored chunk so a scan batch cannot overflow.

    ``max_page_bytes`` is measured on ``page_raw_text`` (the live maximum is
    781,593 bytes).  ``max_chunk_bytes`` is the last-line cap on ``chunks.text``:
    the chunker wraps text to a few kilobytes, so this guards only against a
    future change that stops doing so.  ``assumed_batch_rows`` is the Lance scan
    default, measured to overflow at 512 rows and not at 256; ``safety_factor``
    covers compaction batches up to twice that.
    """

    max_page_bytes: int = 1_048_576
    max_chunk_bytes: int = 65_536
    assumed_batch_rows: int = 1_024
    safety_factor: int = 2

    def __post_init__(self) -> None:
        worst = max(self.max_page_bytes, self.max_chunk_bytes)
        if worst * self.assumed_batch_rows * self.safety_factor > 2**31:
            msg = (
                f"row budget {worst:,} bytes x {self.assumed_batch_rows:,} rows x "
                f"{self.safety_factor} exceeds the 2**31 Arrow offset limit"
            )
            raise ValueError(msg)

    def require_page(self, text: str) -> None:
        """Raise :class:`RowBudgetError` if page text is over ``max_page_bytes``."""
        self._require("page_raw_text", text, self.max_page_bytes)

    def require_chunk(self, text: str) -> None:
        """Raise :class:`RowBudgetError` if chunk text is over ``max_chunk_bytes``."""
        self._require("text", text, self.max_chunk_bytes)

    @staticmethod
    def _require(column: str, text: str, limit: int) -> None:
        # Four bytes is the UTF-8 maximum, so a short string cannot exceed the cap
        # and needs no encode; only a long one is measured exactly.
        if len(text) * 4 <= limit:
            return
        size = len(text.encode("utf-8", errors="surrogatepass"))
        if size > limit:
            msg = f"row budget exceeded: {column} {size:,} bytes > {limit:,}"
            raise RowBudgetError(msg)
