"""Protocol definitions for quarry.

Infrastructure protocols (LanceDB) abstract external libraries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import timedelta

    import pyarrow as pa
    from lancedb.index import FTS, Bitmap, BTree


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
    def create_index(
        self,
        column: str,
        *,
        config: BTree | Bitmap | FTS,
        replace: bool = ...,
    ) -> None: ...
    def add_columns(
        self,
        transforms: dict[str, str],
    ) -> None: ...
    def optimize(self, *, cleanup_older_than: timedelta | None = ...) -> object: ...
    def drop_columns(self, columns: list[str]) -> None: ...
    def stats(self) -> object: ...
    @property
    def schema(self) -> pa.Schema: ...
    @property
    def uri(self) -> str: ...


class LanceQuery(Protocol):
    def limit(self, n: int) -> LanceQuery: ...
    def offset(self, n: int) -> LanceQuery: ...
    def metric(self, metric: str) -> LanceQuery: ...
    def where(self, predicate: str) -> LanceQuery: ...
    def select(self, columns: list[str]) -> LanceQuery: ...
    def bypass_vector_index(self) -> LanceQuery: ...
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
