"""Split a page that exceeds the row budget into consecutive parts, never truncating."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, final

if TYPE_CHECKING:
    from quarry.models import PageContent

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class PageSplitter:
    """Cut oversized pages at the best boundary so every part fits ``max_bytes``.

    The parts concatenate back to the original text exactly: a separator stays at
    the end of the part it closes, so nothing is dropped or rewritten.  A cut
    prefers a blank line, then a newline, then a sentence end, then whitespace,
    and only falls back to a hard cut on a character boundary when the last half
    of the window holds none of them.
    """

    max_bytes: int

    _FILL: ClassVar[float] = 0.5
    _SENTENCE_ENDS: ClassVar[tuple[bytes, ...]] = (b". ", b"! ", b"? ")
    _SPACES: ClassVar[tuple[bytes, ...]] = (b" ", b"\t")

    def __post_init__(self) -> None:
        if self.max_bytes < 4:
            msg = (
                f"max_bytes must hold one UTF-8 character (>= 4), got {self.max_bytes}"
            )
            raise ValueError(msg)

    def expand(self, pages: list[PageContent]) -> list[PageContent]:
        """Return *pages* unchanged, or split and renumbered if any is oversized.

        Pages are logical sections for text and code, so renumbering them
        sequentially for the whole document is sound; it happens only when a
        split did, so every other document keeps its extractor's numbering.
        """
        parts = [(page, self.split(page.text)) for page in pages]
        if all(len(texts) == 1 for _, texts in parts):
            return pages
        flat = [(page, text) for page, texts in parts for text in texts]
        logger.info(
            "split oversized pages: %d pages into %d parts", len(pages), len(flat)
        )
        return [
            replace(page, page_number=number, total_pages=len(flat), text=text)
            for number, (page, text) in enumerate(flat, start=1)
        ]

    def split(self, text: str) -> list[str]:
        """Return *text* as consecutive parts of at most ``max_bytes`` UTF-8 bytes."""
        if len(text) * 4 <= self.max_bytes:
            return [text]
        data = text.encode("utf-8", errors="surrogatepass")
        if len(data) <= self.max_bytes:
            return [text]
        parts: list[str] = []
        start = 0
        while len(data) - start > self.max_bytes:
            cut = self._cut(data, start)
            parts.append(data[start:cut].decode("utf-8", errors="surrogatepass"))
            start = cut
        parts.append(data[start:].decode("utf-8", errors="surrogatepass"))
        return parts

    def _cut(self, data: bytes, start: int) -> int:
        """Return the end of the part that begins at *start*."""
        end = start + self.max_bytes
        while end > start and data[end] & 0xC0 == 0x80:  # inside a character
            end -= 1
        if end == start:  # a single character wider than the budget cannot occur
            return start + self.max_bytes
        floor = start + int(self.max_bytes * self._FILL)
        for cut in (
            self._after(data, (b"\n\n",), floor, end),
            self._after(data, (b"\n",), floor, end),
            self._after(data, self._SENTENCE_ENDS, floor, end),
            self._after(data, self._SPACES, floor, end),
        ):
            if cut > start:
                return cut
        return end

    @staticmethod
    def _after(data: bytes, needles: tuple[bytes, ...], floor: int, end: int) -> int:
        """Return the index just past the last needle in ``[floor, end)``, or 0."""
        best = 0
        for needle in needles:
            found = data.rfind(needle, floor, end)
            if found != -1:
                best = max(best, found + len(needle))
        return best
