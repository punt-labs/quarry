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
    ) -> list[PageContent]: ...

    def ocr_image_bytes(
        self,
        image_bytes: bytes,
        document_name: str,
        document_path: Path,
    ) -> PageContent: ...


class EmbeddingBackend(Protocol):
    """Protocol for text embedding backends."""

    @property
    def dimension(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Returns shape (n, dimension)."""
        ...

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Returns shape (dimension,)."""
        ...
