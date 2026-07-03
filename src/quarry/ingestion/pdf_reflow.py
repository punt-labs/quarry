"""Reflow PyMuPDF text blocks into clean, soft-wrap-joined paragraphs.

Flat ``page.get_text()`` hard-wraps at every visual line. This module rebuilds
paragraphs from ``page.get_text("dict")`` geometry: lines reaching a block's
right margin join (unless they close a sentence before a capital); block
boundaries are paragraph breaks; a lone page-number line in a page margin is
dropped. De-hyphenation is delegated to :class:`~quarry.ingestion.hyphenation`
and page-number chrome detection to :class:`~quarry.ingestion.page_geometry`.

Entry point :meth:`PdfReflow.page_text` (fitz page in, string out, with a
flat-text fallback); :meth:`PdfReflow.from_page_dict` / :meth:`PdfReflow.text`
are the pure, dict-testable core. The OCR path is a separate follow-on.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Self

from quarry.ingestion.hyphenation import Dehyphenator
from quarry.ingestion.page_geometry import PageChrome

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
        """Build a line, or None if the bbox is malformed (skip, don't crash)."""
        bbox = line.get("bbox")
        if not (
            isinstance(bbox, (tuple, list))
            and len(bbox) == 4
            and all(isinstance(coord, (int, float)) for coord in bbox)
        ):
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
        """Whether the line ends in terminal punctuation (trailing quotes ok)."""
        trimmed = self.text.rstrip().rstrip(_CLOSING_CHARS)
        return bool(trimmed) and trimmed[-1] in _TERMINAL_PUNCT

    def precedes_new_sentence(self, following: ReflowLine) -> bool:
        """Return whether this line closes a sentence and the next opens capital."""
        if not self.ends_sentence():
            return False
        head = following.text.strip()
        return bool(head) and head[0].isupper()

    def begins_paragraph_break(
        self, following: ReflowLine, block_right: float, block_width: float
    ) -> bool:
        """Whether this short ragged line ends a paragraph before ``following``."""
        if self.gap_to(block_right) <= _SHORT_LINE_FRACTION * block_width:
            return False
        return self.precedes_new_sentence(following)


@dataclass(frozen=True, slots=True)
class ReflowBlock:
    """A text block: an ordered run of lines forming one or more paragraphs."""

    lines: tuple[ReflowLine, ...]
    _right_margin: float = field(init=False)  # cached once; lines are non-empty
    _width: float = field(init=False)

    def __post_init__(self) -> None:
        right = max(line.x1 for line in self.lines)
        object.__setattr__(self, "_right_margin", right)
        object.__setattr__(self, "_width", right - min(ln.x0 for ln in self.lines))

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
        """Rightmost edge across the block's lines."""
        return self._right_margin

    @property
    def width(self) -> float:
        """Span from the block's leftmost to rightmost edge."""
        return self._width

    @property
    def y_top(self) -> float:
        """Topmost (smallest) y coordinate of the block."""
        return min(line.y0 for line in self.lines)

    @property
    def y_bottom(self) -> float:
        """Bottommost (largest) y coordinate of the block."""
        return max(line.y1 for line in self.lines)

    def is_page_number(self) -> bool:
        """Whether this lone block is a page-number token (years exempt)."""
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
        # Cached _right_margin/_width keep this O(1) per line pair. A full-width
        # line closing a sentence before a capital is a break, not a wrap.
        if previous.reaches_margin(self._right_margin, _MARGIN_TOLERANCE):
            return not previous.precedes_new_sentence(following)
        return not previous.begins_paragraph_break(
            following, self._right_margin, self._width
        )


@dataclass(frozen=True, slots=True)
class PdfReflow:
    """Reflowed view of a single PDF page reconstructed from fitz dict blocks."""

    blocks: tuple[ReflowBlock, ...]
    page_height: float = 0.0  # physical page height; 0 falls back to text span

    @classmethod
    def from_page_dict(cls, page: Any) -> Self:  # fitz get_text("dict"); no stubs
        blocks: list[ReflowBlock] = []
        for raw in page.get("blocks", []):
            if raw.get("type") != 0 or not raw.get("lines"):
                continue
            block = ReflowBlock.from_block_dict(raw)
            if block.lines:
                blocks.append(block)
        height = page.get("height", 0.0)
        page_height = float(height) if isinstance(height, (int, float)) else 0.0
        return cls(blocks=tuple(blocks), page_height=page_height)

    @classmethod
    def page_text(cls, page: Any, *, dict_flags: int | None = None) -> str:
        """Return a page's reflowed text, falling back to flat text if empty.

        An empty reflow (all-numeric page, missing "blocks", only non-text
        blocks) must not silently drop an extractable page: it falls back to flat
        ``get_text()`` with a warning. ``dict_flags`` are forwarded to
        ``get_text("dict", flags=...)`` so the caller can exclude image bytes.
        """
        extra = {"flags": dict_flags} if dict_flags is not None else {}
        reflowed = cls.from_page_dict(page.get_text("dict", **extra)).text()
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
        chrome = PageChrome.for_page(self.blocks, self.page_height)
        paragraphs: list[str] = []
        for index, block in enumerate(self.blocks):
            others = self.blocks[:index] + self.blocks[index + 1 :]
            if chrome.is_droppable(block, others):
                logger.debug(
                    "Stripped page-number block %r", block.lines[0].text.strip()
                )
                continue
            paragraphs.extend(block.paragraphs())
        return "\n\n".join(paragraphs)
