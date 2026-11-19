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
    """Describe the table operations used by Quarry's LanceDB storage adapters."""

    def add(self, data: list[dict[str, object]]) -> None:
        """Append prepared rows to the table."""
        ...

    def search(
        self,
        query: list[float] | str | None = ...,
        query_type: str | None = ...,
    ) -> LanceQuery:
        """Build a vector, text, or unfiltered table query.

        Args:
            query: Vector or text payload; None starts an unfiltered scan.
            query_type: Search mode selected by the LanceDB implementation.
        """
        ...

    def count_rows(self, predicate: str | None = ...) -> int:
        """Count all rows or only those matching the SQL predicate."""
        ...

    def delete(self, predicate: str) -> None:
        """Delete rows selected by a SQL predicate."""
        ...

    def create_index(
        self,
        column: str,
        *,
        config: BTree | Bitmap | FTS,
        replace: bool = ...,
    ) -> None:
        """Build an index for a column, optionally replacing its existing index."""
        ...

    def add_columns(
        self,
        transforms: dict[str, str],
    ) -> None:
        """Add columns computed from SQL expressions keyed by column name."""
        ...

    def optimize(self, *, cleanup_older_than: timedelta | None = ...) -> object:
        """Compact data and optionally remove versions older than the cleanup window."""
        ...

    def drop_columns(self, columns: list[str]) -> None:
        """Remove named columns from the table schema."""
        ...

    def stats(self) -> object:
        """Read implementation-specific table and fragment statistics."""
        ...

    @property
    def schema(self) -> pa.Schema:
        """Expose the table's Arrow field definitions."""
        ...

    @property
    def uri(self) -> str:
        """Expose the storage URI identifying the table."""
        ...


class LanceQuery(Protocol):
    """Describe the fluent query operations used by Quarry's retrieval adapters."""

    def limit(self, n: int) -> LanceQuery:
        """Cap the number of rows produced by this query."""
        ...

    def offset(self, n: int) -> LanceQuery:
        """Skip the first n rows before materializing results."""
        ...

    def metric(self, metric: str) -> LanceQuery:
        """Choose the vector-distance metric for this query."""
        ...

    def where(self, predicate: str) -> LanceQuery:
        """Restrict results with a SQL predicate."""
        ...

    def select(self, columns: list[str]) -> LanceQuery:
        """Project only the named columns into each result row."""
        ...

    def bypass_vector_index(self) -> LanceQuery:
        """Require exact vector scanning instead of approximate index lookup."""
        ...

    def to_list(self) -> list[dict[str, object]]:
        """Materialize the configured query as row dictionaries."""
        ...


class ListTablesResult(Protocol):
    """Expose the table names returned by database enumeration."""

    tables: list[str]


class LanceDB(Protocol):
    """Describe the connection operations used by Quarry's storage facade."""

    def list_tables(self) -> ListTablesResult:
        """Enumerate the connection's visible table names."""
        ...

    def open_table(self, name: str) -> LanceTable:
        """Open an existing table by name."""
        ...

    def create_table(
        self,
        name: str,
        *,
        data: list[dict[str, object]],
        schema: object,
    ) -> LanceTable:
        """Create a named table from initial rows and an explicit schema."""
        ...
