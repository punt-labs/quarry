"""Dispatch table for the text-like ingest formats.

The text-like formats (plain/Markdown/LaTeX/DOCX, source code, spreadsheets,
HTML, presentations) share a single ingest shape and differ only in the
extractor they build, the words they log, and which ``IngestStats`` field counts
their units.  Each is one :class:`TextLikeFormat` instance rather than a copy of
the same forty-line function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.extractors.code_extractor import SUPPORTED_CODE_EXTENSIONS, CodeExtractor
from quarry.extractors.html_extractor import SUPPORTED_HTML_EXTENSIONS, HtmlExtractor
from quarry.extractors.presentation_extractor import (
    SUPPORTED_PRESENTATION_EXTENSIONS,
    PresentationExtractor,
)
from quarry.extractors.spreadsheet_extractor import (
    SUPPORTED_SPREADSHEET_EXTENSIONS,
    SpreadsheetExtractor,
)
from quarry.extractors.text_extractor import SUPPORTED_TEXT_EXTENSIONS, TextExtractor
from quarry.ingestion.ingest_stats import IngestStats

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from quarry.config import Settings
    from quarry.extractors.protocol import FormatExtractor
    from quarry.models import PageContent


@final
@dataclass(frozen=True, slots=True)
class TextLikeFormat:
    """One text-like format: its extractor, progress labels, and stats field."""

    read_verb: str
    unit_label: str
    make_extractor: Callable[[Settings], FormatExtractor]
    make_stats: Callable[[int], IngestStats]

    def extract(
        self, settings: Settings, file_path: Path, document_name: str
    ) -> list[PageContent]:
        """Return the document's pages via this format's extractor."""
        return self.make_extractor(settings).extract_pages(
            file_path, document_name=document_name
        )

    def stats(self, unit_count: int) -> IngestStats:
        """Return the ``IngestStats`` for *unit_count* extracted units."""
        return self.make_stats(unit_count)


_SECTION_FORMAT = TextLikeFormat(
    "Reading", "Sections", lambda _s: TextExtractor(), lambda n: IngestStats(sections=n)
)
_CODE_FORMAT = TextLikeFormat(
    "Parsing",
    "Definitions",
    lambda _s: CodeExtractor(),
    lambda n: IngestStats(definitions=n),
)
_SPREADSHEET_FORMAT = TextLikeFormat(
    "Reading",
    "Sections",
    lambda s: SpreadsheetExtractor(max_chars=s.chunk_max_chars),
    lambda n: IngestStats(sections=n),
)
_HTML_FORMAT = TextLikeFormat(
    "Reading", "Sections", lambda _s: HtmlExtractor(), lambda n: IngestStats(sections=n)
)
_PRESENTATION_FORMAT = TextLikeFormat(
    "Reading",
    "Slides",
    lambda _s: PresentationExtractor(),
    lambda n: IngestStats(slides=n),
)

# Suffix -> handler for every text-like format the pipeline dispatches to; the
# extension sets are disjoint, so a suffix maps to exactly one handler.
TEXT_LIKE_FORMATS: dict[str, TextLikeFormat] = {
    **dict.fromkeys(SUPPORTED_CODE_EXTENSIONS, _CODE_FORMAT),
    **dict.fromkeys(SUPPORTED_TEXT_EXTENSIONS, _SECTION_FORMAT),
    **dict.fromkeys(SUPPORTED_SPREADSHEET_EXTENSIONS, _SPREADSHEET_FORMAT),
    **dict.fromkeys(SUPPORTED_HTML_EXTENSIONS, _HTML_FORMAT),
    **dict.fromkeys(SUPPORTED_PRESENTATION_EXTENSIONS, _PRESENTATION_FORMAT),
}
