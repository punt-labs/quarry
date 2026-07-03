"""Reflow PyMuPDF text blocks into clean, soft-wrap-joined paragraphs.

PyMuPDF's flat ``page.get_text()`` emits one newline per *visual* line, so a
paragraph that wraps across four screen lines arrives hard-wrapped with three
spurious newlines.  This module reconstructs paragraphs from the richer
``page.get_text("dict")`` block/line structure:

* Lines within a block whose right edge reaches the block's right margin are
  soft wraps and are joined (de-hyphenating a trailing line-break hyphen).
* A line that ends well short of the margin, closes a sentence, and precedes a
  capitalised line is a real paragraph break and is kept.
* Block boundaries are paragraph breaks (blank line).
* Standalone page-number lines are dropped, except plausible years.

The public entry point is :class:`PdfReflow`.  ``PdfReflow.from_page_dict`` builds
the value tree from a fitz dict; ``text()`` returns the reflowed plain string.

The de-hyphenation bias mirrors the menu-bar ``ExtractedTextFormatter``: a
visible hyphen is a recoverable error, a merged fake word is not — so when
unsure, keep the hyphen.

The OCR path (``ocr_local.py``) is a separate follow-on; it has no per-line
bounding boxes and needs a weaker y-gap heuristic, tracked separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Self

# Hard compounds whose hyphen is semantic — keep it even across a line break.
_KEEP_HYPHEN_COMPOUNDS: frozenset[str] = frozenset(
    {
        "well-known",
        "well-being",
        "self-aware",
        "self-contained",
        "long-term",
        "short-term",
        "read-only",
        "state-of-the-art",
    }
)

# Single words commonly split at a line break — merge them (strip the hyphen).
_STRIP_MERGE_WORDS: frozenset[str] = frozenset(
    {
        "inasmuch",
        "notwithstanding",
        "nevertheless",
        "nonetheless",
        "throughout",
        "therefore",
        "moreover",
        "furthermore",
        "otherwise",
        "whatsoever",
    }
)

_PAGE_NUMBER_RE = re.compile(r"\d{1,4}")
_TERMINAL_PUNCT = frozenset({".", "!", "?"})

# Plausible year range exempted from page-number stripping (RFC-free heuristic).
_MIN_PLAUSIBLE_YEAR = 1000
_MAX_PLAUSIBLE_YEAR = 2999

# A line ending more than this fraction of the block width short of the right
# margin is "short" for paragraph-break disambiguation.
_SHORT_LINE_FRACTION = 0.15

# Points of slack when deciding a line reached the block's right margin.
_MARGIN_TOLERANCE = 2.0


@dataclass(frozen=True, slots=True)
class ReflowLine:
    """One visual line: its joined span text and horizontal extent."""

    text: str
    x0: float
    x1: float

    @classmethod
    def from_line_dict(cls, line: Any) -> Self:  # fitz line dict; no type stubs
        spans = line.get("spans", [])
        text = "".join(str(span.get("text", "")) for span in spans)
        x0, _y0, x1, _y1 = line["bbox"]
        return cls(text=text, x0=float(x0), x1=float(x1))

    def gap_to(self, block_right: float) -> float:
        """Horizontal distance from this line's right edge to the margin."""
        return block_right - self.x1

    def reaches_margin(self, block_right: float, tolerance: float) -> bool:
        """Return whether this line runs to the block's right margin (a wrap)."""
        return self.gap_to(block_right) <= tolerance

    def ends_sentence(self) -> bool:
        """Return whether the trimmed line ends with terminal punctuation."""
        trimmed = self.text.rstrip()
        return bool(trimmed) and trimmed[-1] in _TERMINAL_PUNCT

    def begins_paragraph_break(
        self, following: ReflowLine, block_right: float, block_width: float
    ) -> bool:
        """Return whether this short line ends a paragraph before ``following``.

        A paragraph ends when the line stops well short of the margin, closes a
        sentence, and the next line opens with a capital — the ragged-right and
        short-final-line signal the bbox margin alone cannot see.
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
            for raw in block["lines"]
            if (line := ReflowLine.from_line_dict(raw)).text.strip()
        )
        return cls(lines=lines)

    @property
    def right_margin(self) -> float:
        """Rightmost edge across the block's lines (lines are non-empty)."""
        return max(line.x1 for line in self.lines)

    @property
    def width(self) -> float:
        """Span from the block's leftmost to rightmost edge."""
        left = min(line.x0 for line in self.lines)
        return self.right_margin - left

    def is_page_number(self) -> bool:
        """Return whether this block is a standalone page number to drop.

        Strips 1-3 digit lines and 4-digit non-years; exempts plausible years
        so 'Annual Report / 2024' keeps its 2024.
        """
        if len(self.lines) != 1:
            return False
        token = self.lines[0].text.strip()
        if not _PAGE_NUMBER_RE.fullmatch(token):
            return False
        if len(token) == 4:
            year = int(token)
            return not (_MIN_PLAUSIBLE_YEAR <= year <= _MAX_PLAUSIBLE_YEAR)
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
                current = self._merge(current, piece)
            previous = line
        if current:
            result.append(current)
        return result

    def _joins(self, previous: ReflowLine, following: ReflowLine) -> bool:
        right = self.right_margin
        if previous.reaches_margin(right, _MARGIN_TOLERANCE):
            return True
        return not previous.begins_paragraph_break(following, right, self.width)

    @staticmethod
    def _merge(accumulated: str, addition: str) -> str:
        if not accumulated:
            return addition
        ends_hyphen = (
            len(accumulated) >= 2
            and accumulated[-1] == "-"
            and accumulated[-2].isalpha()
        )
        if ends_hyphen:
            return ReflowBlock._dehyphenate(accumulated, addition)
        return f"{accumulated} {addition}"

    @staticmethod
    def _dehyphenate(accumulated: str, addition: str) -> str:
        prefix = accumulated[:-1]
        split = len(prefix)
        while split > 0 and prefix[split - 1].isalpha():
            split -= 1
        left, before = prefix[split:], prefix[:split]

        cut = 0
        while cut < len(addition) and addition[cut].isalpha():
            cut += 1
        right, after = addition[:cut], addition[cut:]

        compound = f"{left}-{right}".lower()
        merged = f"{left}{right}".lower()
        if compound not in _KEEP_HYPHEN_COMPOUNDS and merged in _STRIP_MERGE_WORDS:
            joined = f"{left}{right}"
        else:
            joined = f"{left}-{right}"
        return f"{before}{joined}{after}"


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

    def text(self) -> str:
        """Return the page's reflowed text: paragraphs joined by blank lines."""
        paragraphs: list[str] = []
        for block in self.blocks:
            if block.is_page_number():
                continue
            paragraphs.extend(block.paragraphs())
        return "\n\n".join(paragraphs)
