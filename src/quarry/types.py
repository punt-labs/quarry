"""Protocol definitions for quarry.

Infrastructure protocols (LanceDB, Textract, S3) abstract external libraries.
Domain protocols (OcrBackend, EmbeddingBackend) define backend contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    import numpy as np
    from numpy.typing import NDArray

    from quarry.models import PageContent


# --- Infrastructure: LanceDB ---


class LanceTable(Protocol):
    def add(self, data: list[dict[str, object]]) -> None: ...
    def search(
        self,
        query: list[float] | None = ...,
    ) -> LanceQuery: ...
    def count_rows(self, predicate: str | None = ...) -> int: ...
    def delete(self, predicate: str) -> None: ...
    def create_scalar_index(
        self,
        column: str,
        *,
        index_type: str = ...,
        replace: bool = ...,
    ) -> None: ...
    def optimize(self) -> object: ...


class LanceQuery(Protocol):
    def limit(self, n: int) -> LanceQuery: ...
    def where(self, predicate: str) -> LanceQuery: ...
    def select(self, columns: list[str]) -> LanceQuery: ...
    def to_list(self) -> list[dict[str, object]]: ...


class ListTablesResult(Protocol):
    tables: list[str]


class LanceDB(Protocol):
    def list_tables(self) -> ListTablesResult: ...
    def open_table(self, name: str) -> LanceTable: ...
    def create_table(
        self,
        name: str,
        *,
        data: list[dict[str, object]],
        schema: object,
    ) -> LanceTable: ...


# --- Infrastructure: AWS (Textract, S3) ---


class TextractClient(Protocol):
    def detect_document_text(
        self,
        *,
        Document: dict[str, object],  # noqa: N803
    ) -> dict[str, object]: ...

    def start_document_text_detection(
        self,
        *,
        DocumentLocation: dict[str, object],  # noqa: N803
    ) -> dict[str, object]: ...

    def get_document_text_detection(
        self,
        *,
        JobId: str,  # noqa: N803
        NextToken: str | None = ...,  # noqa: N803
    ) -> dict[str, object]: ...


class S3Client(Protocol):
    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
    ) -> None: ...

    def delete_object(
        self,
        *,
        Bucket: str,  # noqa: N803
        Key: str,  # noqa: N803
    ) -> None: ...


# --- Domain: OCR and Embedding backends ---


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
        """OCR multiple pages from a document (PDF or TIFF)."""
        ...

    def ocr_image_bytes(
        self,
        image_bytes: bytes,
        document_name: str,
        document_path: Path,
    ) -> PageContent:
        """OCR a single-page image from bytes."""
        ...


class EmbeddingBackend(Protocol):
    """Protocol for text embedding backends."""

    @property
    def dimension(self) -> int: ...

    @property
    def model_name(self) -> str: ...

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Embed a batch of texts. Returns shape (n, dimension)."""
        ...

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Embed a search query. Returns shape (dimension,)."""
        ...
