"""The extraction result type and the strategy Protocol that produces it (PY-IC-9)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from quarry.config import Settings
    from quarry.ingestion.ingest_context import Progress
    from quarry.ingestion.ingest_stats import IngestStats
    from quarry.models import PageContent


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Pages, per-format stats, and the source_format tag for one document."""

    pages: list[PageContent]
    stats: IngestStats
    source_format: str


class FormatStrategy(Protocol):
    """Extracts one document's pages from its file path (PY-DP-11: one method)."""

    def extract(
        self,
        settings: Settings,
        file_path: Path,
        document_name: str,
        progress: Progress,
        /,
    ) -> ExtractedDocument: ...
