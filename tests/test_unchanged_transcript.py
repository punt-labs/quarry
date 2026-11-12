"""Tests for quarry.ingestion.unchanged_transcript: version + completeness gate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from quarry.db.chunk_store import ChunkStore
from quarry.ingestion.unchanged_transcript import UnchangedTranscript
from quarry.models import Chunk

if TYPE_CHECKING:
    from quarry.db import Database

_NAME = "session-abcd1234"
_COLLECTION = "repo-captures"
_DIM = 768


def _chunk(*, page_number: int, total_pages: int, version: str) -> Chunk:
    return Chunk(
        document_name=_NAME,
        document_path="",
        collection=_COLLECTION,
        page_number=page_number,
        total_pages=total_pages,
        chunk_index=0,
        text="hello",
        page_raw_text="hello",
        page_type="text",
        source_format="inline",
        ingestion_timestamp=datetime.now(UTC),
        source_version=version,
    )


def _store_pages(
    store: ChunkStore, pages: list[int], *, total_pages: int, version: str
) -> None:
    chunks = [
        _chunk(page_number=p, total_pages=total_pages, version=version) for p in pages
    ]
    vectors = np.zeros((len(chunks), _DIM), dtype=np.float32)
    store.insert(chunks, vectors)


class TestPartialInsertIsNeverSkipped:
    """quarry-uq95 finding 1: a version match on a partial document must re-ingest.

    A windowed insert that failed partway through leaves surviving rows
    correctly tagged with the version being written, but short of the
    document's full page count. The unfixed gate (version equality alone)
    treats that as "already stored" and skips forever -- the missing pages
    are never repaired because a retry of the unchanged file carries the
    same version every time.
    """

    def test_a_version_match_on_an_incomplete_document_still_reingests(
        self, database: Database
    ) -> None:
        # Only page 1 of 2 landed -- the insert died before page 2's window.
        _store_pages(
            database.store, [1], total_pages=2, version="500000:1700000000000000000"
        )

        found = UnchangedTranscript.look_for(
            _NAME, "500000:1700000000000000000", _COLLECTION, database
        )

        assert found is None, (
            "a version match on a document missing half its pages was treated "
            "as unchanged and skipped -- the gap can never be repaired by a "
            "retry carrying the same version"
        )

    def test_a_version_match_on_a_complete_document_is_skipped(
        self, database: Database
    ) -> None:
        # Both pages landed -- the document is whole at this version.
        _store_pages(
            database.store, [1, 2], total_pages=2, version="500000:1700000000000000000"
        )

        found = UnchangedTranscript.look_for(
            _NAME, "500000:1700000000000000000", _COLLECTION, database
        )

        assert found is not None
        assert found.skip().get("skipped") == "unchanged"

    def test_a_different_version_always_reingests_even_if_complete(
        self, database: Database
    ) -> None:
        _store_pages(database.store, [1, 2], total_pages=2, version="v1")

        found = UnchangedTranscript.look_for(_NAME, "v2", _COLLECTION, database)

        assert found is None

    def test_no_stored_document_reingests(self, database: Database) -> None:
        found = UnchangedTranscript.look_for(_NAME, "v1", _COLLECTION, database)

        assert found is None
