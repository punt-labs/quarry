"""Classify text as binary content that must never be indexed.

Neutral and stdlib-only so both the HTTP routes and the ingestion pipeline can
import it without crossing a layer.  Classification is per *document*: a
legitimate PDF extraction can hold a short page of math symbols that is over a
third non-text, so a per-page ratio would reject real documents.  Only pages
large enough for the ratio to be statistically meaningful are checked on their
own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, final

from quarry.capture_marker import CaptureMarker
from quarry.refusal import RefusalError


class UnindexableContentError(RefusalError):
    """Raised when content cannot be indexed; the message is the stable reason.

    Messages never embed a URL or document name, so they are safe to log and to
    return to a client verbatim: the failures that carry one (a fetch, a crawl)
    are replaced by a URL-free reason before they reach here.
    """


@final
@dataclass(frozen=True, slots=True)
class BinaryContent:
    """Reject text that is really a binary payload decoded as text.

    A document is binary when it opens with a known signature, when the share
    of U+FFFD and C0 control characters (excluding tab, newline, carriage return
    and form feed) over the whole document exceeds ``max_nontext_ratio``, or when
    one page of ``LARGE_PAGE_CHARS`` or more exceeds the ratio on its own.  NUL
    characters count toward the ratio but are not a criterion by themselves:
    legitimate PDF extractions contain a few.

    A signature made only of printable characters (``%PDF-``, ``GIF8``) is a
    suspicion, not proof: prose can open with it.  It rejects only when the
    sampled text also holds ``SOFT_EVIDENCE`` non-text characters.
    """

    max_nontext_ratio: float = 0.25

    # A page this long (65,536 characters) is classified on its own as well as in
    # the document aggregate.  Measured over a corpus of real extractions, no
    # legitimate page of this size or more exceeded a 0.12 non-text ratio, while
    # the one binary page (a PDF stored as text) was millions of characters at
    # 0.52.  Below the limit a dense page is indistinguishable from legitimate
    # math or tables, so only the document aggregate judges it.
    LARGE_PAGE_CHARS: ClassVar[int] = 64 * 1024
    # Characters taken from each of the head, middle and tail of a request body by
    # ``sniff``; three windows keep the check independent of the body size.
    SAMPLE_WINDOW_CHARS: ClassVar[int] = 32 * 1024
    SOFT_EVIDENCE: ClassVar[int] = 4

    _NONTEXT: ClassVar[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\ufffd]")
    # Text-decoded forms of each signature: the raw bytes (latin-1) and the
    # U+FFFD form a lossy UTF-8 decode produces for the high bytes.
    _HARD: ClassVar[tuple[tuple[str, str], ...]] = (
        ("PK\x03\x04", "zip archive header"),
        ("\x89PNG", "png header"),
        ("\ufffdPNG", "png header"),
        ("\xff\xd8\xff", "jpeg header"),
        ("\ufffd\ufffd\ufffd", "undecodable binary header"),
        ("\x7fELF", "elf header"),
    )
    _SOFT: ClassVar[tuple[tuple[str, str], ...]] = (
        ("%PDF-", "pdf header"),
        ("GIF8", "gif header"),
    )

    def __post_init__(self) -> None:
        # Written so NaN fails: every comparison with it is False.
        if not 0 < self.max_nontext_ratio < 1:
            msg = f"max_nontext_ratio must be in (0, 1), got {self.max_nontext_ratio}"
            raise ValueError(msg)

    def require_document(self, pages: Sequence[str]) -> None:
        """Raise :class:`UnindexableContentError` if the page texts are binary."""
        if not pages:
            return
        self._require_text_start(pages[0])
        total = sum(len(page) for page in pages)
        nontext = 0
        for number, page in enumerate(pages, start=1):
            bad = self._count_nontext(page)
            nontext += bad
            if len(page) >= self.LARGE_PAGE_CHARS and self._exceeds(bad, len(page)):
                raise UnindexableContentError(
                    self._ratio_message(bad, len(page), f"page {number} non-text")
                )
        if self._exceeds(nontext, total):
            raise UnindexableContentError(self._ratio_message(nontext, total))

    def sniff(self, content: str) -> None:
        """Raise if *content* looks binary, judging at most three windows of it.

        Meant for request bodies that can be tens of megabytes: the signature
        and the head, middle and tail samples are judged together, so the cost
        is independent of the body size.  A body of three windows or fewer is
        judged whole.
        """
        self._require_text_start(content)
        sample = self._sample(content)
        bad = self._count_nontext(sample)
        if self._exceeds(bad, len(sample)):
            raise UnindexableContentError(self._ratio_message(bad, len(sample)))

    def _require_text_start(self, text: str) -> None:
        """Raise if *text* opens with a binary signature (soft ones need evidence)."""
        head = CaptureMarker.without(text[:512]).lstrip()
        for prefix, label in self._HARD:
            if head.startswith(prefix):
                raise UnindexableContentError(f"binary content rejected: {label}")
        for prefix, label in self._SOFT:
            if (
                head.startswith(prefix)
                and self._count_nontext(self._sample(text)) >= self.SOFT_EVIDENCE
            ):
                raise UnindexableContentError(f"binary content rejected: {label}")

    @classmethod
    def _sample(cls, content: str) -> str:
        window = cls.SAMPLE_WINDOW_CHARS
        if len(content) <= window * 3:
            return content
        middle = (len(content) - window) // 2
        return content[:window] + content[middle : middle + window] + content[-window:]

    def _count_nontext(self, text: str) -> int:
        return sum(1 for _ in self._NONTEXT.finditer(text))

    def _exceeds(self, nontext: int, total: int) -> bool:
        return nontext > self.max_nontext_ratio * total

    def _ratio_message(
        self, nontext: int, total: int, subject: str = "non-text"
    ) -> str:
        return (
            f"binary content rejected: {subject} ratio "
            f"{nontext / total:.2f} exceeds {self.max_nontext_ratio:.2f}"
        )
