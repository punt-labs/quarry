"""The byte budget one stored row may take, and the invariant that bounds a batch.

Arrow ``Utf8`` offsets are 32-bit, so a decode batch whose string column totals
2**31 bytes or more fails with ``Offset overflow`` (the failure that made
``table.optimize()`` raise on a PDF stored as 831 rows of 4.5 MB each).  LanceDB's
default scan batch is 1,024 rows, so a row's worst case is bounded here and the
arithmetic is checked on construction rather than left in a comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, final

from quarry.content_guard import UnindexableContentError

if TYPE_CHECKING:
    from collections.abc import Mapping


class RowBudgetError(UnindexableContentError):
    """Raised when a row's text exceeds the budget; the text is never truncated.

    A refusal like any other unindexable content: the message names the column
    and sizes only, never a value, so a task ends ``failed`` with it and one
    quiet WARN.  Callers that wrap it (a sync log line) may add a document name,
    which is a raw URL for a plain ingest, so the wrapped text is not URL-free.
    """


@final
@dataclass(frozen=True, slots=True)
class RowBudget:
    """Cap a stored page, chunk and label so a scan batch cannot overflow.

    ``max_page_bytes`` is measured on ``page_raw_text``; one byte under 1 MiB so
    the invariant holds strictly.  ``max_chunk_bytes`` is the last-line cap on
    ``chunks.text``: the chunker wraps text to ``chunk_max_chars``, which
    ``Settings`` bounds, so this guards only against a change that stops doing
    so.

    ``max_label_bytes`` caps each label column (``summary``, ``document_name``,
    ``document_path``, ``collection``, ``agent_handle``).  A label is copied onto
    every chunk of a document: 8 KiB is twice a ``PATH_MAX`` path and far above
    any one-line summary, yet five such columns over a 1,024-row batch total
    40 MiB, where an uncapped 3 MiB summary on 700 chunks overflowed the batch.

    ``assumed_batch_rows`` is the Lance scan default; on a table holding
    multi-megabyte rows a scan overflowed at 512 rows and not at 256.
    ``safety_factor`` is headroom: the budget holds for batches up to that many
    times larger than assumed.
    """

    max_page_bytes: int = 2**20 - 1
    max_chunk_bytes: int = 65_536
    max_label_bytes: int = 8_192
    assumed_batch_rows: int = 1_024
    safety_factor: int = 2

    _ARROW_OFFSET_LIMIT: ClassVar[int] = 2**31 - 1
    LABEL_COLUMNS: ClassVar[tuple[str, ...]] = (
        "summary",
        "document_name",
        "document_path",
        "collection",
        "agent_handle",
    )

    def __post_init__(self) -> None:
        for name in (
            "max_page_bytes",
            "max_label_bytes",
            "assumed_batch_rows",
            "safety_factor",
        ):
            if getattr(self, name) < 1:
                msg = f"{name} must be >= 1, got {getattr(self, name)}"
                raise ValueError(msg)
        if self.max_chunk_bytes < 4:
            msg = f"max_chunk_bytes must be >= 4, got {self.max_chunk_bytes}"
            raise ValueError(msg)
        worst = max(self.max_page_bytes, self.max_chunk_bytes, self.max_label_bytes)
        if (
            worst * self.assumed_batch_rows * self.safety_factor
            > self._ARROW_OFFSET_LIMIT
        ):
            msg = (
                f"row budget {worst:,} bytes x {self.assumed_batch_rows:,} rows x "
                f"{self.safety_factor} exceeds the 2**31 - 1 Arrow offset limit"
            )
            raise ValueError(msg)

    @property
    def max_chunk_chars(self) -> int:
        """Return the most characters a chunk may hold (four bytes each at worst)."""
        return self.max_chunk_bytes // 4

    def require_page(self, text: str) -> None:
        """Raise :class:`RowBudgetError` if page text is over ``max_page_bytes``."""
        self._require("page_raw_text", text, self.max_page_bytes)

    def require_chunk(self, text: str) -> None:
        """Raise :class:`RowBudgetError` if chunk text is over ``max_chunk_bytes``."""
        self._require("text", text, self.max_chunk_bytes)

    def require_label(self, column: str, text: str) -> None:
        """Raise :class:`RowBudgetError` if label *text* is over ``max_label_bytes``."""
        self._require(column, text, self.max_label_bytes)

    def require_labels(self, row: Mapping[str, object]) -> None:
        """Raise :class:`RowBudgetError` if a label column of *row* is over the cap."""
        for column in self.LABEL_COLUMNS:
            self.require_label(column, str(row[column]))

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
