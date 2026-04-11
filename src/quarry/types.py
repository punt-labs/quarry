"""Protocol definitions for quarry.

Infrastructure protocols (LanceDB) abstract external libraries.
Domain protocols (OcrBackend, EmbeddingBackend) define backend contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import timedelta
    from pathlib import Path

    import numpy as np
    import pyarrow as pa
    from numpy.typing import NDArray

    from quarry.models import PageContent


# --- Infrastructure: LanceDB ---


class LanceTable(Protocol):
    def add(self, data: list[dict[str, object]]) -> None: ...
    def search(
        self,
        query: list[float] | str | None = ...,
        query_type: str | None = ...,
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
    def create_fts_index(
        self,
        column: str,
        *,
        replace: bool = ...,
    ) -> None: ...
    def add_columns(
        self,
        transforms: dict[str, str],
    ) -> None: ...
    def optimize(self, *, cleanup_older_than: timedelta | None = ...) -> object: ...
    @property
    def schema(self) -> pa.Schema: ...


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
