from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray


class EmbeddingModel(Protocol):
    def encode(
        self,
        sentences: str | list[str],
        *,
        normalize_embeddings: bool = ...,
        show_progress_bar: bool = ...,
        prompt_name: str | None = ...,
    ) -> NDArray[np.float32]: ...


class LanceTable(Protocol):
    def add(self, data: list[dict[str, object]]) -> None: ...
    def search(
        self,
        query: list[float] | None = ...,
    ) -> LanceQuery: ...
    def count_rows(self) -> int: ...
    def delete(self, predicate: str) -> None: ...


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


class TextractClient(Protocol):
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
