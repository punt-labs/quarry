"""Protocol defining the contract for format-specific page extraction."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from quarry.models import PageContent


@runtime_checkable
class FormatExtractor(Protocol):
    """Extract pages from a document file.

    Every format-specific extractor (text, code, HTML, spreadsheet,
    presentation) satisfies this protocol.  The common interface is a
    single method that takes a file path, an optional document name
    override, and returns a list of ``PageContent`` objects.
    """

    def extract_pages(
        self,
        path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Extract logical pages from *path*.

        Args:
            path: Filesystem path to the document.
            document_name: Override for the stored document name.
                Defaults to ``path.name`` when ``None``.

        Returns:
            One ``PageContent`` per logical page or section.
        """
        ...
