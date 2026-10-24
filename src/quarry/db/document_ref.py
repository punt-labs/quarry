"""Scoped selectors for a collection's chunks and for one document's chunks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self, final

from quarry._sql import escape_sql


@final
@dataclass(frozen=True, slots=True)
class CollectionRef:
    """A collection, named, as the SQL clause that selects its chunks."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            msg = "CollectionRef.name must be non-empty"
            raise ValueError(msg)

    @classmethod
    def optional(cls, name: str | None) -> Self | None:
        """Return the ref for *name*, or ``None`` when no collection is meant.

        The one place an absent or empty collection name is normalized, so a
        :class:`DocumentRef` has exactly one encoding of "unscoped".
        """
        return cls(name) if name else None

    def clause(self) -> str:
        """Return the clause selecting every chunk of this collection."""
        return f"collection = '{escape_sql(self.name)}'"


@final
@dataclass(frozen=True, slots=True)
class DocumentRef:
    """A scoped selector for a document's chunks.

    Owns the SQL predicate that selects the document's rows. ``min_chunk_index``
    of 0 selects the whole document; a positive watermark restricts to the resume
    tail ``chunk_index >= min_chunk_index`` (DES-034 §5.3), so a single value
    object drives both full-document and tail deletes.
    """

    document_name: str
    # None is the selector's own meaning, "any collection", not a missing value.
    collection: CollectionRef | None = None
    min_chunk_index: int = 0

    def __post_init__(self) -> None:
        if not self.document_name:
            msg = "DocumentRef.document_name must be non-empty"
            raise ValueError(msg)
        if self.min_chunk_index < 0:
            msg = f"min_chunk_index must be >= 0, got {self.min_chunk_index}"
            raise ValueError(msg)

    @classmethod
    def tail(cls, document_name: str, collection: str | None, watermark: int) -> Self:
        """Return the ref for the resume tail from *watermark* (which must be > 0)."""
        return cls(
            document_name, CollectionRef.optional(collection), watermark
        ).require_tail()

    def require_tail(self) -> Self:
        """Return this ref, or raise if it selects the whole document, not a tail.

        A tail delete from watermark 0 would drop the whole document, so the one
        check lives here for both :meth:`tail` and ``delete_document_tail``.
        """
        if self.min_chunk_index <= 0:
            msg = f"a tail needs a positive watermark, got {self.min_chunk_index}"
            raise ValueError(msg)
        return self

    def predicate(self) -> str:
        """Return the document/collection/watermark selection clause."""
        clause = f"document_name = '{escape_sql(self.document_name)}'"
        if self.collection is not None:
            clause += f" AND {self.collection.clause()}"
        if self.min_chunk_index > 0:
            clause += f" AND chunk_index >= {self.min_chunk_index}"
        return clause
