"""Classify text as binary content that must never be indexed.

Neutral and stdlib-only so both the HTTP routes and the ingestion pipeline can
import it without crossing a layer.  Classification is per *document*: a
legitimate PDF extraction can hold a short page of math symbols that is 34%
non-text (measured: ``books/z-manual.pdf`` page 163, 677 characters), so a
per-page ratio would reject real documents.  Only pages large enough for the
ratio to be statistically meaningful are checked on their own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, final

# A page this long is classified on its own as well as inside the document
# aggregate.  Measured over every page of the live database (91,499 pages): no
# legitimate page of 64 KiB or more exceeds a 0.119 non-text ratio, while the one
# binary page (a PDF stored as text) is 2.4 million characters at 0.52.  Below
# the limit a dense page is indistinguishable from legitimate math or tables, so
# only the document aggregate judges it.
LARGE_PAGE_CHARS = 64 * 1024

# Characters sampled from the head, middle and tail of a request body by
# ``BinaryContent.sniff``; three windows keep the check O(1) in body size.
SAMPLE_WINDOW_CHARS = 32 * 1024


class UnindexableContentError(ValueError):
    """Raised when content cannot be indexed; the message is the stable reason.

    Messages never embed a URL or document name, so they are safe to log and to
    return to a client verbatim.
    """


class UnsupportedContentError(UnindexableContentError):
    """Raised before the body is read when the media type is not indexable text.

    The message carries no URL, so ``"content rejected:"`` is safe to log and to
    return to a client verbatim.
    """


@final
@dataclass(frozen=True, slots=True)
class BinaryContent:
    """Reject text that is really a binary payload decoded as text.

    A document is binary when it opens with a known magic prefix, when the share
    of U+FFFD and C0 control characters (excluding tab, newline, carriage return
    and form feed) over the whole document exceeds ``max_nontext_ratio``, or when
    one page of ``LARGE_PAGE_CHARS`` or more exceeds the ratio on its own.  NUL
    characters count toward the ratio but are not a criterion by themselves:
    legitimate PDF extractions contain a few.
    """

    max_nontext_ratio: float = 0.25

    _MARKER: ClassVar[re.Pattern[str]] = re.compile(
        r"\A<!-- media_type: [^\n]*? -->\n?"
    )
    _NONTEXT: ClassVar[re.Pattern[str]] = re.compile("[\x00-\x08\x0b\x0e-\x1f\ufffd]")
    # Text-decoded forms of each signature: the raw bytes (latin-1) and the
    # U+FFFD form a lossy UTF-8 decode produces for the high bytes.
    _MAGIC: ClassVar[tuple[tuple[str, str], ...]] = (
        ("%PDF-", "pdf header"),
        ("PK\x03\x04", "zip archive header"),
        ("\x89PNG", "png header"),
        ("\ufffdPNG", "png header"),
        ("GIF8", "gif header"),
        ("\xff\xd8\xff", "jpeg header"),
        ("\ufffd\ufffd\ufffd", "jpeg header"),
        ("\x7fELF", "elf header"),
    )

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
            if len(page) >= LARGE_PAGE_CHARS and self._exceeds(bad, len(page)):
                raise UnindexableContentError(
                    self._ratio_message(bad, len(page), f"page {number} non-text")
                )
        if self._exceeds(nontext, total):
            raise UnindexableContentError(self._ratio_message(nontext, total))

    def sniff(self, content: str) -> None:
        """Raise if *content* looks binary, reading at most three windows of it.

        Meant for request bodies that can be tens of megabytes: the magic prefix
        plus the head, middle and tail samples are judged together, so the cost
        is independent of the body size.  A short body is judged whole.
        """
        self._require_text_start(content)
        sample = self._sample(content)
        bad = self._count_nontext(sample)
        if self._exceeds(bad, len(sample)):
            raise UnindexableContentError(self._ratio_message(bad, len(sample)))

    def _require_text_start(self, text: str) -> None:
        """Raise if *text* opens with a known binary signature."""
        head = self._MARKER.sub("", text[:512], count=1).lstrip()
        for prefix, label in self._MAGIC:
            if head.startswith(prefix):
                raise UnindexableContentError(f"binary content rejected: {label}")

    @staticmethod
    def _sample(content: str) -> str:
        window = SAMPLE_WINDOW_CHARS
        if len(content) <= window * 3:
            return content
        middle = (len(content) - window) // 2
        return content[:window] + content[middle : middle + window] + content[-window:]

    def _count_nontext(self, text: str) -> int:
        return len(self._NONTEXT.findall(text))

    def _exceeds(self, nontext: int, total: int) -> bool:
        return nontext > self.max_nontext_ratio * total

    def _ratio_message(
        self, nontext: int, total: int, subject: str = "non-text"
    ) -> str:
        return (
            f"binary content rejected: {subject} ratio "
            f"{nontext / total:.2f} exceeds {self.max_nontext_ratio:.2f}"
        )
