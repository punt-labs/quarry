"""A single-URL ingest without overwrite is create-if-absent; cleanup owns its name."""

from __future__ import annotations

from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from quarry.config import Settings
from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.ingest_jobs import IngestJob
from quarry.db.facade import Database
from quarry.http_url import HttpUrl
from quarry.ingestion.bulk_ingest import BulkOptions
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.sitemap_ingest import ingest_auto
from quarry.ingestion.streaming import DocumentStreamer
from quarry.ingestion.web_ingest import InlineIngest, ingest_content
from quarry.web_fetch_name import WebFetchName
from tests.fakes import FakeEmbeddingBackend, patched_embedder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.db.chunk_store import ChunkStore
    from quarry.models import Chunk

_COLLECTION = "c"
_URL = "https://example.com/page"
_BODY = "<p>" + "a sentence of text. " * 400 + "</p>"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "lancedb"
    path.mkdir()
    with patched_embedder(FakeEmbeddingBackend(768)):
        yield Database.connect(path)


def _names(db: Database) -> set[str]:
    return {d["document_name"] for d in db.catalog.list_documents(_COLLECTION)}


def _page(db: Database, name: str, *, overwrite: bool) -> None:
    ingest_content(
        InlineIngest(_BODY, format_hint="html", url_sourced=True),
        name,
        Progress(None),
        IngestContext(db, Settings(), overwrite=overwrite, collection=_COLLECTION),
    )


def _chunks(db: Database, name: str) -> int:
    docs = [
        d for d in db.catalog.list_documents(_COLLECTION) if d["document_name"] == name
    ]
    return sum(d["chunk_count"] for d in docs)


def _failing_insert() -> AbstractContextManager[object]:
    real = DocumentStreamer.insert

    def partial_then_fail(
        self: DocumentStreamer, chunks: list[Chunk], store: ChunkStore, name: str
    ) -> int:
        real(self, chunks[:1], store, name)  # one window lands
        msg = "second window failed"
        raise RuntimeError(msg)

    return patch.object(DocumentStreamer, "insert", partial_then_fail)


class TestAFailedInsertOnlyCleansUpWhatItOwns:
    def test_a_good_prior_page_survives_a_failed_non_overwriting_insert(
        self, db: Database
    ) -> None:
        _page(db, _URL, overwrite=False)
        before = _chunks(db, _URL)
        assert before > 0
        with _failing_insert(), pytest.raises(RuntimeError, match="second"):
            _page(db, _URL, overwrite=False)
        assert _chunks(db, _URL) >= before  # nothing of the good copy was deleted

    def test_a_partial_write_to_a_new_name_is_still_removed(self, db: Database) -> None:
        with _failing_insert(), pytest.raises(RuntimeError, match="second"):
            _page(db, _URL, overwrite=False)
        assert _URL not in _names(db)

    def test_an_overwriting_failed_insert_removes_the_partial_write(
        self, db: Database
    ) -> None:
        _page(db, _URL, overwrite=False)
        with _failing_insert(), pytest.raises(RuntimeError, match="second"):
            _page(db, _URL, overwrite=True)
        assert _URL not in _names(db)  # the prior copy was deleted on purpose


class TestASinglePlainIngestIsCreateIfAbsent:
    @staticmethod
    def _run(db: Database, *, overwrite: bool) -> object:
        context = IngestContext(
            db, Settings(), overwrite=overwrite, collection=_COLLECTION
        )
        return ingest_auto(HttpUrl(_URL), Progress(None), context, BulkOptions())

    def test_an_existing_page_is_skipped_without_a_fetch(self, db: Database) -> None:
        _page(db, _URL, overwrite=False)
        before = _chunks(db, _URL)
        with (
            patch("quarry.sitemap.SitemapDiscovery.discover_pages", return_value=[]),
            patch("quarry.ingestion.sitemap_ingest.ingest_url") as fetch,
        ):
            result = self._run(db, overwrite=False)
        fetch.assert_not_called()
        assert result == {
            "document_name": HttpUrl(_URL).document_name,
            "collection": _COLLECTION,
            "chunks": 0,
            "skipped": "exists",
        }
        assert _chunks(db, _URL) == before  # no duplicate chunks appended

    def test_a_new_page_is_fetched(self, db: Database) -> None:
        with (
            patch("quarry.sitemap.SitemapDiscovery.discover_pages", return_value=[]),
            patch(
                "quarry.ingestion.sitemap_ingest.ingest_url",
                return_value={"chunks": 3},
            ) as fetch,
        ):
            self._run(db, overwrite=False)
        fetch.assert_called_once()

    def test_overwrite_fetches_even_when_the_page_exists(self, db: Database) -> None:
        _page(db, _URL, overwrite=False)
        with (
            patch("quarry.sitemap.SitemapDiscovery.discover_pages", return_value=[]),
            patch(
                "quarry.ingestion.sitemap_ingest.ingest_url",
                return_value={"chunks": 3},
            ) as fetch,
        ):
            self._run(db, overwrite=True)
        fetch.assert_called_once()


class TestAScrubbedSingleIngestIsCreateIfAbsent:
    @staticmethod
    def _job(*, overwrite: bool) -> IngestJob:
        return IngestJob(
            source=HttpUrl(_URL),
            scrub=True,
            labels=IngestLabels(
                overwrite=overwrite,
                collection=_COLLECTION,
                agent_handle="",
                memory_type="",
                summary="",
            ),
        )

    def test_an_existing_capture_is_skipped_without_a_fetch(self, db: Database) -> None:
        _page(db, WebFetchName.of(_URL), overwrite=False)
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        with patch(
            "quarry.daemon.captured_body_ingest.CapturedBodyIngest.run"
        ) as fetch:
            result = self._job(overwrite=False)._ingest(ctx)  # pyright: ignore[reportPrivateUsage]
        fetch.assert_not_called()
        assert result["skipped"] == "exists"
        assert result["chunks"] == 0

    def test_overwrite_fetches_even_when_the_capture_exists(self, db: Database) -> None:
        _page(db, WebFetchName.of(_URL), overwrite=False)
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        with patch(
            "quarry.daemon.captured_body_ingest.CapturedBodyIngest.run",
            return_value={"chunks": 2},
        ) as fetch:
            self._job(overwrite=True)._ingest(ctx)  # pyright: ignore[reportPrivateUsage]
        fetch.assert_called_once()
