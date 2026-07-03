"""Reflow PyMuPDF text blocks into clean, soft-wrap-joined paragraphs.

Flat ``page.get_text()`` emits one newline per *visual* line, so a wrapped
paragraph arrives hard-wrapped. This module rebuilds paragraphs from the richer
``page.get_text("dict")`` block/line structure: lines reaching a block's right
margin are soft wraps and join (de-hyphenating a line-break hyphen); a short
sentence-closing line before a capital is a real break; block boundaries are
paragraph breaks; a lone page-number line in the top/bottom margin is dropped
(years exempt), while a numeric block in the body is kept. De-hyphenation of a
line-break hyphen is delegated to :class:`~quarry.ingestion.hyphenation.Dehyphenator`.

Entry points: :meth:`PdfReflow.page_text` (fitz page in, string out, with a
flat-text fallback) and :meth:`PdfReflow.from_page_dict` / :meth:`PdfReflow.text`
for the pure, dict-testable core. The OCR path (``ocr_local.py``) lacks per-line
bounding boxes and is a separate follow-on.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Self

from quarry.ingestion.hyphenation import Dehyphenator

logger = logging.getLogger(__name__)

_PAGE_NUMBER_RE = re.compile(r"\d{1,4}")
_TERMINAL_PUNCT = frozenset({".", "!", "?"})
# Trailing quotes/brackets stripped before the terminal-punct test, so a
# sentence ending .' or .") still reads as terminal. The u201d/u2019 escapes
# are the curly close double and single quotes.
_CLOSING_CHARS = "\"'\u201d\u2019)]}"

_MIN_PLAUSIBLE_YEAR = 1000  # years exempt from page-number strip
_MAX_PLAUSIBLE_YEAR = 2999
_SHORT_LINE_FRACTION = 0.15  # a line this far short of the margin is "short"
_MARGIN_TOLERANCE = 2.0  # points of slack for "reached the right margin"
_MARGIN_BAND_FRACTION = 0.12  # top/bottom band counted as page margin


@dataclass(frozen=True, slots=True)
class ReflowLine:
    """One visual line: its joined span text and bounding box."""

    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_line_dict(cls, line: Any) -> Self | None:  # fitz line dict; no stubs
        """Build a line from a fitz dict, or None if its bbox is malformed.

        None (not a raise) means one broken line is skipped, not the document.
        """
        bbox = line.get("bbox")
        if not (isinstance(bbox, (tuple, list)) and len(bbox) == 4):
            logger.debug("Skipping line with malformed bbox: %r", bbox)
            return None
        spans = line.get("spans", [])
        text = "".join(str(span.get("text", "")) for span in spans)
        x0, y0, x1, y1 = bbox
        return cls(text=text, x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1))

    def gap_to(self, block_right: float) -> float:
        """Horizontal distance from this line's right edge to the margin."""
        return block_right - self.x1

    def reaches_margin(self, block_right: float, tolerance: float) -> bool:
        """Return whether this line runs to the block's right margin (a wrap)."""
        return self.gap_to(block_right) <= tolerance

    def ends_sentence(self) -> bool:
        """Return whether the line ends with terminal punctuation.

        Trailing quotes/brackets are stripped first, so .' and .") count.
        """
        trimmed = self.text.rstrip().rstrip(_CLOSING_CHARS)
        return bool(trimmed) and trimmed[-1] in _TERMINAL_PUNCT

    def begins_paragraph_break(
        self, following: ReflowLine, block_right: float, block_width: float
    ) -> bool:
        """Return whether this short line ends a paragraph before ``following``.

        True when the line stops well short of the margin, closes a sentence, and
        the next line opens with a capital — the signal the bbox margin misses.
        """
        if self.gap_to(block_right) <= _SHORT_LINE_FRACTION * block_width:
            return False
        if not self.ends_sentence():
            return False
        head = following.text.strip()
        return bool(head) and head[0].isupper()


@dataclass(frozen=True, slots=True)
class ReflowBlock:
    """A text block: an ordered run of lines forming one or more paragraphs."""

    lines: tuple[ReflowLine, ...]

    @classmethod
    def from_block_dict(cls, block: Any) -> Self:  # fitz block dict; no stubs
        lines = tuple(
            line
            for raw in block.get("lines", [])
            if (line := ReflowLine.from_line_dict(raw)) is not None
            and line.text.strip()
        )
        return cls(lines=lines)

    @property
    def right_margin(self) -> float:
        """Rightmost edge across the block's lines (lines are non-empty)."""
        return max(line.x1 for line in self.lines)

    @property
    def width(self) -> float:
        """Span from the block's leftmost to rightmost edge."""
        return self.right_margin - min(line.x0 for line in self.lines)

    @property
    def y_top(self) -> float:
        """Topmost (smallest) y coordinate of the block."""
        return min(line.y0 for line in self.lines)

    @property
    def y_bottom(self) -> float:
        """Bottommost (largest) y coordinate of the block."""
        return max(line.y1 for line in self.lines)

    def in_margin(self, page_top: float, page_bottom: float) -> bool:
        """Return whether the block sits in the top or bottom page margin band."""
        height = page_bottom - page_top
        if height <= 0:
            return True
        band = _MARGIN_BAND_FRACTION * height
        return self.y_top <= page_top + band or self.y_bottom >= page_bottom - band

    def is_page_number(self) -> bool:
        """Return whether this lone block is a page-number token.

        1-3 digit lines and 4-digit non-years match; plausible years are exempt.
        Position is gated by :meth:`in_margin` so a body table cell survives.
        """
        if len(self.lines) != 1:
            return False
        token = self.lines[0].text.strip()
        if not _PAGE_NUMBER_RE.fullmatch(token):
            return False
        if len(token) == 4:
            return not (_MIN_PLAUSIBLE_YEAR <= int(token) <= _MAX_PLAUSIBLE_YEAR)
        return True

    def paragraphs(self) -> list[str]:
        """Join soft-wrapped lines into paragraphs, splitting at real breaks."""
        result: list[str] = []
        current = ""
        previous: ReflowLine | None = None
        for line in self.lines:
            piece = line.text.strip()
            if previous is not None and not self._joins(previous, line):
                result.append(current)
                current = piece
            else:
                current = Dehyphenator.merge(current, piece)
            previous = line
        if current:
            result.append(current)
        return result

    def _joins(self, previous: ReflowLine, following: ReflowLine) -> bool:
        right = self.right_margin
        if previous.reaches_margin(right, _MARGIN_TOLERANCE):
            return True
        return not previous.begins_paragraph_break(following, right, self.width)


@dataclass(frozen=True, slots=True)
class PdfReflow:
    """Reflowed view of a single PDF page reconstructed from fitz dict blocks."""

    blocks: tuple[ReflowBlock, ...]

    @classmethod
    def from_page_dict(cls, page: Any) -> Self:  # fitz get_text("dict"); no stubs
        blocks: list[ReflowBlock] = []
        for raw in page.get("blocks", []):
            if raw.get("type") != 0 or not raw.get("lines"):
                continue
            block = ReflowBlock.from_block_dict(raw)
            if block.lines:
                blocks.append(block)
        return cls(blocks=tuple(blocks))

    @classmethod
    def page_text(cls, page: Any) -> str:  # fitz page; no stubs
        """Return a page's reflowed text, falling back to flat text if empty.

        An empty reflow — an all-numeric page, a missing "blocks" key, only
        non-text blocks — must not silently drop a page that has extractable
        text. When reflow is empty but flat ``get_text()`` is not, the flat text
        is returned and a warning logged so the fallback is auditable.
        """
        reflowed = cls.from_page_dict(page.get_text("dict")).text()
        if reflowed.strip():
            return reflowed
        flat = str(page.get_text())
        if flat.strip():
            logger.warning("Reflow yielded empty text; falling back to flat get_text()")
            return flat
        return reflowed

    def text(self) -> str:
        """Return the page's reflowed text: paragraphs joined by blank lines."""
        if not self.blocks:
            return ""
        page_top = min(block.y_top for block in self.blocks)
        page_bottom = max(block.y_bottom for block in self.blocks)
        paragraphs: list[str] = []
        for block in self.blocks:
            if block.is_page_number() and block.in_margin(page_top, page_bottom):
                logger.debug(
                    "Stripped page-number block %r", block.lines[0].text.strip()
                )
                continue
            paragraphs.extend(block.paragraphs())
        return "\n\n".join(paragraphs)
