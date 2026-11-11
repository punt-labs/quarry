"""Chunk catalog: list documents, list collections, get page text."""

from __future__ import annotations

import logging
from typing import Self

from quarry._sql import escape_sql
from quarry.db.document_summary_builder import DocumentSummaryBuilder
from quarry.db.document_version import DocumentVersion
from quarry.db.schema import TABLE_NAME
from quarry.results import CollectionSummary, CoverageCounts, DocumentSummary
from quarry.types import LanceDB, LanceTable

logger = logging.getLogger(__name__)

# table.search() without a vector defaults to a limited scan (typically 10
# rows).  Non-vector filtered queries must set an explicit limit large enough
# to cover the full table so the WHERE clause is evaluated against every row.
_FULL_SCAN_LIMIT = 1_000_000
# The columns a single document's version-and-completeness check needs.
_VERSION_COLUMNS = ("total_pages", "page_number", "source_version")

# A compaction transcript is filed under ``session-<session_id[:8]>``
# (api/capture_ingest.py); a URL capture is not.  The session archive is the
# only capture-collection row surfaced in the coverage line — a fetched URL is
# background metadata, not a user-visible artefact.
_TRANSCRIPT_PREFIX = "session-"


class ChunkCatalog:
    """Query chunk metadata: documents, collections, and page text."""

    __slots__ = ("_db",)

    _db: LanceDB

    def __new__(cls, db: LanceDB) -> Self:
        self = super().__new__(cls)
        self._db = db
        return self

    @staticmethod
    def _table_or_none(db: LanceDB) -> LanceTable | None:
        """Return *db*'s chunks table, or ``None`` when it does not exist yet.

        Every read in this class starts here — an absent table means an
        empty answer, never an error (the collection simply has nothing
        indexed).
        """
        if TABLE_NAME not in db.list_tables().tables:
            return None
        return db.open_table(TABLE_NAME)

    def list_documents(
        self,
        collection_filter: str | None = None,
    ) -> list[DocumentSummary]:
        """List all indexed documents with metadata."""
        table = self._table_or_none(self._db)
        if table is None:
            return []

        query = table.search().limit(_FULL_SCAN_LIMIT)
        query = query.select(DocumentSummaryBuilder.columns(table.schema.names))
        if collection_filter:
            query = query.where(f"collection = '{escape_sql(collection_filter)}'")
        return DocumentSummaryBuilder.group_by_name(query.to_list())

    def document_version(self, name: str, collection: str) -> DocumentVersion:
        """Return *name*'s shared version and page completeness in *collection*.

        Reads only the columns an unchanged-capture check needs, filtered by
        both ``document_name`` and ``collection`` — unlike
        :meth:`list_documents`, which materializes every document in the
        collection to answer one document's question. A table not yet
        migrated predates ``source_version``; its rows report none.
        """
        table = self._table_or_none(self._db)
        if table is None:
            return DocumentVersion.absent()
        columns = [c for c in _VERSION_COLUMNS if c in table.schema.names]
        rows = (
            table.search()
            .where(self._by_name_and_collection(name, collection))
            .limit(_FULL_SCAN_LIMIT)
            .select(columns)
            .to_list()
        )
        return DocumentVersion.of_rows(rows)

    @staticmethod
    def _by_name_and_collection(name: str, collection: str) -> str:
        """Return the ``document_name``+``collection`` predicate every exact
        single-document lookup in this file shares."""
        return (
            f"document_name = '{escape_sql(name)}'"
            f" AND collection = '{escape_sql(collection)}'"
        )

    def list_collections(self) -> list[CollectionSummary]:
        """List all collections with document and chunk counts."""
        table = self._table_or_none(self._db)
        if table is None:
            return []

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

    def coverage(self, collection: str, captures_collection: str) -> CoverageCounts:
        """Return per-repo counts split across ``collection`` and its captures sibling.

        ``documents_indexed`` counts distinct non-memory documents in
        ``collection``; ``memories_saved`` counts distinct memory rows
        (``agent_handle`` or ``memory_type`` set) in the same collection;
        ``transcripts_captured`` counts distinct ``session-`` compaction archives
        in ``captures_collection``. The ``WHERE collection IN (...)`` predicate
        bounds the scan to this repo's slice of the shared catalog — cheap
        enough for the SessionStart hot path.
        """
        empty: CoverageCounts = {
            "documents_indexed": 0,
            "transcripts_captured": 0,
            "memories_saved": 0,
        }
        table = self._table_or_none(self._db)
        if table is None:
            return empty
        predicate = (
            f"collection IN ('{escape_sql(collection)}', "
            f"'{escape_sql(captures_collection)}')"
        )
        rows = (
            table.search()
            .where(predicate)
            .limit(_FULL_SCAN_LIMIT)
            .select(["collection", "document_name", "agent_handle", "memory_type"])
            .to_list()
        )
        documents: set[str] = set()
        memories: set[str] = set()
        transcripts: set[str] = set()
        for row in rows:
            name = str(row["document_name"])
            if str(row["collection"]) == captures_collection:
                if name.startswith(_TRANSCRIPT_PREFIX):
                    transcripts.add(name)
                continue
            if row["agent_handle"] or row["memory_type"]:
                memories.add(name)
            else:
                documents.add(name)
        return {
            "documents_indexed": len(documents),
            "transcripts_captured": len(transcripts),
            "memories_saved": len(memories),
        }

    def document_exists(self, document_name: str, collection: str) -> bool:
        """Return whether *document_name* has any indexed chunk in *collection*.

        ``limit()`` bounds the SCAN, not the filtered match count, so
        ``limit(1)`` can miss a real match. Use the same full-scan limit as
        every other filtered read in this file.
        """
        table = self._table_or_none(self._db)
        if table is None:
            return False
        predicate = self._by_name_and_collection(document_name, collection)
        query = table.search().where(predicate).limit(_FULL_SCAN_LIMIT)
        return bool(query.select(["document_name"]).to_list())

    def get_page_text(
        self,
        document_name: str,
        page_number: int,
        collection: str | None = None,
    ) -> str | None:
        """Retrieve the full raw text for a specific page."""
        logger.debug("get_page_text: document=%s, page=%d", document_name, page_number)

        table = self._table_or_none(self._db)
        if table is None:
            logger.debug("get_page_text: table %s not found", TABLE_NAME)
            return None

        predicate = (
            f"document_name = '{escape_sql(document_name)}'"
            f" AND page_number = {page_number}"
        )
        if collection:
            predicate += f" AND collection = '{escape_sql(collection)}'"

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
