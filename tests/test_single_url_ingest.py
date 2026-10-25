"""A single-URL ingest without overwrite is create-if-absent; cleanup owns its name."""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from quarry.config import Settings
from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.ingest_labels import IngestLabels
from quarry.db.facade import Database
from quarry.http_url import HttpUrl
from quarry.ingestion.bulk_ingest import BulkOptions
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.sitemap_ingest import ingest_auto
from quarry.ingestion.streaming import DocumentStreamer
from quarry.ingestion.web_ingest import InlineIngest, ingest_content
from quarry.web_fetch_name import WebFetchName
from tests.fakes import FakeEmbeddingBackend, patched_embedder
from tests.url_names import store_as_an_older_build_did

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.db.chunk_store import ChunkStore
    from quarry.models import Chunk
    from quarry.results import IngestResult

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


def _ingest(db: Database, name: str, *, overwrite: bool) -> IngestResult:
    return ingest_content(
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


class TestAFetchedPageOwnsItsNameWhenItIsInserted:
    def test_a_held_page_is_skipped_and_nothing_is_inserted(self, db: Database) -> None:
        _page(db, _URL, overwrite=False)
        before = _chunks(db, _URL)
        assert before > 0
        with patch.object(DocumentStreamer, "insert") as insert:
            result = _ingest(db, _URL, overwrite=False)
        insert.assert_not_called()
        assert result.get("skipped") == "exists"
        assert _chunks(db, _URL) == before  # the good copy is exactly as it was

    def test_a_failed_lookup_surfaces_before_anything_is_deleted(
        self, db: Database
    ) -> None:
        _page(db, _URL, overwrite=False)
        before = _chunks(db, _URL)
        with (
            patch.object(
                type(db.catalog), "document_exists", side_effect=RuntimeError("lookup")
            ),
            pytest.raises(RuntimeError, match="lookup"),
        ):
            _page(db, _URL, overwrite=False)
        assert _chunks(db, _URL) == before

    def test_an_overwrite_reads_nothing_between_the_delete_and_the_insert(
        self, db: Database
    ) -> None:
        _page(db, _URL, overwrite=False)
        with patch.object(
            type(db.catalog), "document_exists", side_effect=RuntimeError("lookup")
        ):
            _page(db, _URL, overwrite=True)  # a read here could lose the page
        assert _chunks(db, _URL) > 0

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


def _job(url: str = _URL, *, overwrite: bool) -> IngestJob:
    return IngestJob(
        source=HttpUrl(url),
        scrub=True,
        labels=IngestLabels(
            overwrite=overwrite,
            collection=_COLLECTION,
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )


def _run(job: IngestJob, db: Database) -> dict[str, object]:
    ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
    return job._ingest(ctx)  # pyright: ignore[reportPrivateUsage]


class TestAScrubbedSingleIngestIsCreateIfAbsent:
    _job = staticmethod(_job)

    def test_an_existing_capture_is_skipped_without_a_fetch(self, db: Database) -> None:
        _page(db, WebFetchName.of(_URL), overwrite=False)
        with patch(
            "quarry.daemon.captured_body_ingest.CapturedBodyIngest.run"
        ) as fetch:
            result = _run(self._job(overwrite=False), db)
        fetch.assert_not_called()
        assert result.get("skipped") == "exists"
        assert result["chunks"] == 0

    def test_overwrite_fetches_even_when_the_capture_exists(self, db: Database) -> None:
        _page(db, WebFetchName.of(_URL), overwrite=False)
        with patch(
            "quarry.daemon.captured_body_ingest.CapturedBodyIngest.run",
            return_value={"chunks": 2},
        ) as fetch:
            _run(self._job(overwrite=True), db)
        fetch.assert_called_once()


_LEGACY = "https://example.com/café"
_CANONICAL = "https://example.com/caf%C3%A9"


class TestASkipNamesWhatTheStoreHolds:
    def test_the_plain_single_page_door_reports_the_legacy_spelling(
        self, db: Database
    ) -> None:
        store_as_an_older_build_did(db, _LEGACY, collection=_COLLECTION)
        context = IngestContext(db, Settings(), overwrite=False, collection=_COLLECTION)
        with (
            patch("quarry.sitemap.SitemapDiscovery.discover_pages", return_value=[]),
            patch("quarry.ingestion.sitemap_ingest.ingest_url") as fetch,
        ):
            result = ingest_auto(
                HttpUrl(_CANONICAL), Progress(None), context, BulkOptions()
            )
        fetch.assert_not_called()
        assert cast("dict[str, object]", result)["skipped"] == "exists"
        assert cast("dict[str, object]", result)["document_name"] == _LEGACY

    def test_the_scrubbed_door_reports_the_legacy_spelling(self, db: Database) -> None:
        store_as_an_older_build_did(db, _LEGACY, collection=_COLLECTION)
        with patch(
            "quarry.daemon.captured_body_ingest.CapturedBodyIngest.run"
        ) as fetch:
            result = _run(_job(_CANONICAL, overwrite=False), db)
        fetch.assert_not_called()
        assert result["skipped"] == "exists"
        assert result["document_name"] == _LEGACY

    def test_the_funnel_reports_the_canonical_name_when_that_is_held(
        self, db: Database
    ) -> None:
        _page(db, _URL, overwrite=False)
        result = _ingest(db, _URL, overwrite=False)
        assert result["document_name"] == _URL


class TestAnUnderivableNameFailsTheTask:
    def test_it_is_not_read_as_a_name_that_is_free(self, db: Database) -> None:
        with (
            patch(
                "quarry.ingestion.existing_page.WebFetchName.of",
                side_effect=ValueError("malformed URL"),
            ),
            patch("quarry.daemon.captured_body_ingest.CapturedBodyIngest.run") as fetch,
            pytest.raises(ValueError, match="malformed URL"),
        ):
            _run(_job(overwrite=False), db)
        fetch.assert_not_called()


class TestASkipLeavesATrace:
    def test_one_info_line_names_the_redacted_page_and_the_way_out(
        self, db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        url = "https://example.com/private/page"
        _page(db, url, overwrite=False)
        with caplog.at_level(logging.INFO, logger="quarry.ingestion.existing_page"):
            _ingest(db, url, overwrite=False)
        [record] = [
            r for r in caplog.records if r.name == "quarry.ingestion.existing_page"
        ]
        assert record.levelno == logging.INFO
        assert "already stored; pass --overwrite to refresh" in record.getMessage()
        assert "private" not in record.getMessage()
