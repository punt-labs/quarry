"""Domain protocols for the OCR and embedding backends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import PageContent


class OcrBackend(Protocol):
    """Protocol for OCR backends that extract text from documents."""

    def ocr_document(
        self,
        document_path: Path,
        page_numbers: list[int],
        total_pages: int,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Recognize selected pages of a PDF or TIFF document.

        Args:
            page_numbers: One-based page numbers to recognize.
            total_pages: Original document page count used in output metadata.
            document_name: Stored name override; None uses the filename.

        Returns:
            Recognized pages; unavailable local OCR may yield an empty list.

        Raises:
            ValueError: The document format is unsupported.
            OSError: The document cannot be opened or decoded.
        """
        ...

    def ocr_image_bytes(
        self,
        image_bytes: bytes,
        document_name: str,
        document_path: Path,
    ) -> PageContent:
        """Recognize a single encoded image and retain its document attribution.

        Returns:
            One page; unavailable local OCR may produce a page with empty text.

        Raises:
            OSError: The encoded image cannot be decoded.
        """
        ...


class EmbeddingBackend(Protocol):
    """Protocol for text embedding backends."""

    @property
    def dimension(self) -> int:
        """Expose the length of each embedding vector produced by this backend."""
        ...

    @property
    def model_name(self) -> str:
        """Expose the model identifier used for embedding attribution."""
        ...

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Embed texts as rows of shape (n, dimension)."""
        ...

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Embed a query as a vector of shape (dimension,)."""
        ...
