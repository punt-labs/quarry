"""A scoped selector for one document's chunks."""

from __future__ import annotations

from dataclasses import dataclass

from quarry._sql import escape_sql


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """A scoped selector for a document's chunks.

    Owns the SQL predicate that selects the document's rows. ``min_chunk_index``
    of 0 selects the whole document; a positive watermark restricts to the resume
    tail ``chunk_index >= min_chunk_index`` (DES-034 §5.3), so a single value
    object drives both full-document and tail deletes.
    """

    document_name: str
    collection: str | None = None
    min_chunk_index: int = 0

    def __post_init__(self) -> None:
        if not self.document_name:
            msg = "DocumentRef.document_name must be non-empty"
            raise ValueError(msg)
        if self.min_chunk_index < 0:
            msg = f"min_chunk_index must be >= 0, got {self.min_chunk_index}"
            raise ValueError(msg)

    @staticmethod
    def collection_clause(collection: str) -> str:
        """Return the clause selecting every chunk of *collection*."""
        return f"collection = '{escape_sql(collection)}'"

    def predicate(self) -> str:
        """Return the document/collection/watermark selection clause."""
        clause = f"document_name = '{escape_sql(self.document_name)}'"
        if self.collection:
            clause += f" AND {self.collection_clause(self.collection)}"
        if self.min_chunk_index > 0:
            clause += f" AND chunk_index >= {self.min_chunk_index}"
        return clause
