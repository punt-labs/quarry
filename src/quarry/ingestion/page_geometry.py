"""Page-geometry policy for detecting page-number chrome to drop.

A lone 1-4 digit block is only chrome (a header/footer page number) when it sits
in the physical page's top or bottom margin *and* is vertically isolated from the
surrounding content. Measuring against the physical page — not the extracted
text span — and requiring isolation keeps a numeric table cell or a statistic
near the bottom of the body from being mistaken for a footer and dropped.

The block geometry is consumed structurally; ``ReflowBlock`` is imported only for
annotations to avoid a circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from quarry.ingestion.pdf_reflow import ReflowBlock

# Fraction of page height counted as the top/bottom margin band.
_MARGIN_BAND_FRACTION = 0.12
# A page number must be at least this fraction of page height away from the
# nearest content block to count as isolated chrome.
_ISOLATION_GAP_FRACTION = 0.05


@dataclass(frozen=True, slots=True)
class PageChrome:
    """Decides which blocks are droppable page-number chrome for one page."""

    _page_top: float
    _page_bottom: float

    @classmethod
    def for_page(cls, blocks: tuple[ReflowBlock, ...], page_height: float) -> Self:
        """Build from the physical page height, or the text span if unknown."""
        if page_height > 0:
            return cls(_page_top=0.0, _page_bottom=page_height)
        top = min(block.y_top for block in blocks)
        bottom = max(block.y_bottom for block in blocks)
        return cls(_page_top=top, _page_bottom=bottom)

    def is_droppable(self, block: ReflowBlock, others: tuple[ReflowBlock, ...]) -> bool:
        """A lone page-number block, in the margin band and isolated, is chrome."""
        return (
            block.is_page_number()
            and self._in_margin(block)
            and self._is_isolated(block, others)
        )

    def _in_margin(self, block: ReflowBlock) -> bool:
        span = self._page_bottom - self._page_top
        if span <= 0:
            return True
        band = _MARGIN_BAND_FRACTION * span
        return (
            block.y_top <= self._page_top + band
            or block.y_bottom >= self._page_bottom - band
        )

    def _is_isolated(self, block: ReflowBlock, others: tuple[ReflowBlock, ...]) -> bool:
        if not others:
            return True
        span = self._page_bottom - self._page_top
        nearest = min(self._gap(block, other) for other in others)
        return nearest >= _ISOLATION_GAP_FRACTION * span

    @staticmethod
    def _gap(first: ReflowBlock, second: ReflowBlock) -> float:
        """Vertical gap between two blocks, 0 if they overlap vertically."""
        if first.y_bottom <= second.y_top:
            return second.y_top - first.y_bottom
        if second.y_bottom <= first.y_top:
            return first.y_top - second.y_bottom
        return 0.0
