"""Group a document's raw chunk rows into its :class:`DocumentSummary`."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from quarry.results import DocumentSummary

if TYPE_CHECKING:
    from collections.abc import Sequence


@final
class DocumentSummaryBuilder:
    """Build one document's :class:`~quarry.results.DocumentSummary` from its rows.

    Column selection and row grouping were previously free functions in
    :class:`~quarry.db.chunk_catalog.ChunkCatalog` that took no catalog state
    — they belong here, next to the summary shape they build.
    """

    _BASE_COLUMNS = (
        "document_name",
        "document_path",
        "collection",
        "total_pages",
        "page_number",
        "ingestion_timestamp",
    )

    __slots__ = ()

    @classmethod
    def columns(cls, present: list[str]) -> list[str]:
        """Return the columns a summary reads that *present* (the table) holds.

        A table not yet migrated predates ``source_version``; its rows report
        none.
        """
        wanted = [*cls._BASE_COLUMNS, "source_version"]
        return [column for column in wanted if column in present]

    @classmethod
    def group_by_name(cls, rows: list[dict[str, object]]) -> list[DocumentSummary]:
        """Group *rows* by ``document_name`` and summarise each group."""
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(str(row["document_name"]), []).append(row)
        return [cls._summary(name, chunks) for name, chunks in grouped.items()]

    @classmethod
    def _summary(cls, name: str, chunks: list[dict[str, object]]) -> DocumentSummary:
        """Summarise document *name* from its *chunks* rows."""
        first = chunks[0]
        return {
            "document_name": name,
            "document_path": str(first["document_path"]),
            "collection": str(first["collection"]),
            "total_pages": int(str(first["total_pages"])),
            "chunk_count": len(chunks),
            "indexed_pages": len({int(str(c["page_number"])) for c in chunks}),
            "ingestion_timestamp": max(str(c["ingestion_timestamp"]) for c in chunks),
            "source_version": cls._shared_version(chunks),
        }

    @staticmethod
    def _shared_version(chunks: Sequence[dict[str, object]]) -> str:
        """Return the ``source_version`` all *chunks* carry; ``""`` when they differ.

        Two overwrites of one document that interleave can leave rows from both;
        such a document vouches for no single version.
        """
        versions = {str(c.get("source_version", "")) for c in chunks}
        return versions.pop() if len(versions) == 1 else ""
