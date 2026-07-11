"""Per-format ingest counters merged into an ingest result."""

from __future__ import annotations

from dataclasses import dataclass, fields


@dataclass(frozen=True, slots=True)
class IngestStats:
    """Optional per-format counters merged into an :class:`IngestResult`.

    Each ingest path fills only the counters that apply to its format — pages
    for PDFs, sections for text, slides for decks — and ``as_result_fields``
    drops the unset ones so the result carries just what was measured.
    """

    total_pages: int | None = None
    text_pages: int | None = None
    image_pages: int | None = None
    sections: int | None = None
    definitions: int | None = None
    sheets: int | None = None
    slides: int | None = None
    file_format: str | None = None

    def as_result_fields(self) -> dict[str, int | str]:
        """Return the set counters keyed as they appear in the result."""
        present: dict[str, int | str] = {}
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None:
                key = "format" if field.name == "file_format" else field.name
                present[key] = value
        return present
