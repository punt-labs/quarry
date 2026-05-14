"""Chunk catalog: list documents, list collections, get page text."""

from __future__ import annotations

import logging
from typing import Self

from quarry._sql import escape_sql
from quarry.results import CollectionSummary, DocumentSummary
from quarry.schema import TABLE_NAME
from quarry.types import LanceDB

logger = logging.getLogger(__name__)

# table.search() without a vector defaults to a limited scan (typically 10
# rows).  Non-vector filtered queries must set an explicit limit large enough
# to cover the full table so the WHERE clause is evaluated against every row.
_FULL_SCAN_LIMIT = 1_000_000


class ChunkCatalog:
    """Query chunk metadata: documents, collections, and page text."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    def list_documents(
        self,
        collection_filter: str | None = None,
    ) -> list[DocumentSummary]:
        """List all indexed documents with metadata."""
        if TABLE_NAME not in self._db.list_tables().tables:
            return []

        table = self._db.open_table(TABLE_NAME)
        query = (
            table.search()
            .limit(_FULL_SCAN_LIMIT)
            .select(
                [
                    "document_name",
                    "document_path",
                    "collection",
                    "total_pages",
                    "page_number",
                    "ingestion_timestamp",
                ]
            )
        )
        if collection_filter:
            query = query.where(f"collection = '{escape_sql(collection_filter)}'")
        rows = query.to_list()

        if not rows:
            return []

        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            name = str(row["document_name"])
            if name not in grouped:
                grouped[name] = []
            grouped[name].append(row)

        docs: list[DocumentSummary] = []
        for name, chunks in grouped.items():
            pages = {int(str(c["page_number"])) for c in chunks}
            docs.append(
                {
                    "document_name": name,
                    "document_path": str(chunks[0]["document_path"]),
                    "collection": str(chunks[0]["collection"]),
                    "total_pages": int(str(chunks[0]["total_pages"])),
                    "chunk_count": len(chunks),
                    "indexed_pages": len(pages),
                    "ingestion_timestamp": str(
                        max(str(c["ingestion_timestamp"]) for c in chunks)
                    ),
                }
            )

        return docs

    def list_collections(self) -> list[CollectionSummary]:
        """List all collections with document and chunk counts."""
        if TABLE_NAME not in self._db.list_tables().tables:
            return []

        table = self._db.open_table(TABLE_NAME)
        rows = (
            table.search()
            .limit(_FULL_SCAN_LIMIT)
            .select(["collection", "document_name"])
            .to_list()
        )
        if not rows:
            return []

        grouped: dict[str, set[str]] = {}
        counts: dict[str, int] = {}
        for row in rows:
            col = str(row["collection"])
            if col not in grouped:
                grouped[col] = set()
                counts[col] = 0
            grouped[col].add(str(row["document_name"]))
            counts[col] += 1

        return [
            {
                "collection": col,
                "document_count": len(docs),
                "chunk_count": counts[col],
            }
            for col, docs in sorted(grouped.items())
        ]

    def get_page_text(
        self,
        document_name: str,
        page_number: int,
        collection: str | None = None,
    ) -> str | None:
        """Retrieve the full raw text for a specific page."""
        logger.debug("get_page_text: document=%s, page=%d", document_name, page_number)

        if TABLE_NAME not in self._db.list_tables().tables:
            logger.debug("get_page_text: table %s not found", TABLE_NAME)
            return None

        predicate = (
            f"document_name = '{escape_sql(document_name)}'"
            f" AND page_number = {page_number}"
        )
        if collection:
            predicate += f" AND collection = '{escape_sql(collection)}'"

        table = self._db.open_table(TABLE_NAME)
        results = (
            table.search()
            .where(predicate)
            .limit(_FULL_SCAN_LIMIT)
            .select(["page_raw_text"])
            .to_list()
        )

        if not results:
            logger.debug("get_page_text: no results found")
            return None
        return str(results[0]["page_raw_text"])
