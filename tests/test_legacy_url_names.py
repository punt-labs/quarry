"""Tests for quarry.ingestion.legacy_url_names: an old-named document is replaced.

Earlier rounds changed what a stored URL name is (query, fragment and ``;params``
dropped, default ports removed, path percent-encoded).  A re-ingest must replace
the document an older build stored under the old spelling, not orphan it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from quarry.config import Settings
from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.db import Database
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.legacy_url_names import LegacyUrlNames
from quarry.ingestion.web_ingest import InlineIngest, UrlIngest, ingest_content
from quarry.models import Chunk
from tests.fakes import FakeEmbeddingBackend, patched_embedder
from tests.url_names import with_query

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from quarry.daemon.context import DaemonContext
    from quarry.results import IngestResult

_COLLECTION = "c"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    path = tmp_path / "lancedb"
    path.mkdir()
    with patched_embedder(FakeEmbeddingBackend(768)):
        yield Database.connect(path)


def _store_as_stored_by_an_older_build(db: Database, name: str) -> None:
    """Write a row under *name* exactly, as a build that spelled it differently did."""
    chunk = Chunk(
        document_name=name,
        document_path="",
        collection=_COLLECTION,
        page_number=1,
        total_pages=1,
        chunk_index=0,
        text="old body",
        page_raw_text="old body",
        page_type="text",
        source_format="inline",
        ingestion_timestamp=datetime.now(tz=UTC),
    )
    vector = FakeEmbeddingBackend(768).embed_texts(["old body"])
    db.store.insert([chunk], vector)


def _store(db: Database, name: str, *, overwrite: bool = True) -> None:
    ingest_content(
        InlineIngest("some indexable body text for the document"),
        name,
        Progress(None),
        IngestContext(db, Settings(), overwrite=overwrite, collection=_COLLECTION),
    )


def _names(db: Database) -> set[str]:
    return {d["document_name"] for d in db.catalog.list_documents(_COLLECTION)}


class TestOverwriteReplacesTheLegacyName:
    def test_a_legacy_iri_is_replaced_by_its_encoded_form(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        _store(db, "https://example.com/caf%C3%A9")
        assert _names(db) == {"https://example.com/caf%C3%A9"}

    def test_a_legacy_raw_query_form_is_replaced_by_its_digest_form(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page?utm=1")
        _store(db, "https://example.com/page?utm=1")
        assert _names(db) == {with_query("https://example.com/page", "utm=1")}

    def test_a_page_with_another_query_is_another_document(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page?page=1")
        _store(db, "https://example.com/page?page=2")
        assert _names(db) == {
            "https://example.com/page?page=1",
            with_query("https://example.com/page", "page=2"),
        }

    def test_a_page_without_a_query_does_not_replace_one_with_a_query(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page?utm=1")
        _store(db, "https://example.com/page")
        assert _names(db) == {
            "https://example.com/page?utm=1",
            "https://example.com/page",
        }

    def test_a_legacy_default_port_form_is_replaced(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com:443/page")
        _store(db, "https://example.com/page")
        assert _names(db) == {"https://example.com/page"}

    @pytest.mark.parametrize(
        "legacy", ["https://Example.com/page", "https://EXAMPLE.COM:443/page"]
    )
    def test_a_legacy_upper_case_host_is_replaced(
        self, db: Database, legacy: str
    ) -> None:
        _store_as_stored_by_an_older_build(db, legacy)
        _store(db, "https://example.com/page")
        assert _names(db) == {"https://example.com/page"}

    def test_a_different_host_is_untouched(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://other.example/café")
        _store(db, "https://example.com/caf%C3%A9")
        assert _names(db) == {
            "https://other.example/café",
            "https://example.com/caf%C3%A9",
        }

    def test_a_different_path_is_untouched(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/other?utm=1")
        _store(db, "https://example.com/page")
        assert _names(db) == {
            "https://example.com/other?utm=1",
            "https://example.com/page",
        }

    def test_a_non_url_name_is_untouched(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "notes.txt")
        _store(db, "https://example.com/page")
        assert _names(db) == {"notes.txt", "https://example.com/page"}

    def test_the_replacement_is_logged_with_the_redacted_legacy_form(
        self, db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page?token=SECRET")
        with caplog.at_level(logging.INFO):
            _store(db, "https://example.com/page?token=SECRET")
        replaced = [
            r.getMessage() for r in caplog.records if "legacy" in r.getMessage()
        ]
        assert len(replaced) == 1
        assert "https://example.com/…(segments=1)" in replaced[0]
        assert "SECRET" not in caplog.text

    def test_a_non_overwriting_ingest_deletes_nothing(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page?utm=1")
        _store(db, "https://example.com/page", overwrite=False)
        assert _names(db) == {
            "https://example.com/page?utm=1",
            "https://example.com/page",
        }


class TestTheLegacyNameIsTheSameDocument:
    def test_aliases_lists_only_names_that_canonicalize_to_it(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        _store_as_stored_by_an_older_build(db, "https://example.com/caf%C3%A9?x=1")
        _store_as_stored_by_an_older_build(db, "https://example.com/other")
        names = LegacyUrlNames(db)
        assert names.aliases("https://example.com/caf%C3%A9", _COLLECTION) == [
            "https://example.com/café"
        ]
        assert sorted(
            names.aliases(
                with_query("https://example.com/caf%C3%A9", "x=1"), _COLLECTION
            )
        ) == ["https://example.com/caf%C3%A9?x=1"]

    def test_exists_sees_the_legacy_spelling(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        names = LegacyUrlNames(db)
        assert names.exists("https://example.com/caf%C3%A9", _COLLECTION)
        assert not names.exists("https://example.com/missing", _COLLECTION)

    def test_a_name_in_another_collection_does_not_count(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        assert not LegacyUrlNames(db).exists(
            "https://example.com/caf%C3%A9", "elsewhere"
        )

    def test_canonical_form_of_a_url_and_of_a_plain_name(self) -> None:
        assert LegacyUrlNames.canonical("https://Example.com:443/café?x=1") == (
            with_query("https://example.com/caf%C3%A9", "x=1")
        )
        assert LegacyUrlNames.canonical("notes.txt") == "notes.txt"


class TestANameUrlsplitCannotParse:
    """A scrubbed host (``[REDACTED:hostname]``) reads as a broken IPv6 bracket."""

    _SCRUBBED = "https://[REDACTED:hostname]/page"

    def test_it_has_no_legacy_spelling(self, db: Database) -> None:
        assert LegacyUrlNames(db).aliases(self._SCRUBBED, _COLLECTION) == []

    def test_it_exists_only_if_stored_under_exactly_that_name(
        self, db: Database
    ) -> None:
        names = LegacyUrlNames(db)
        assert not names.exists(self._SCRUBBED, _COLLECTION)
        _store_as_stored_by_an_older_build(db, self._SCRUBBED)
        assert names.exists(self._SCRUBBED, _COLLECTION)

    def test_a_scrubbed_capture_of_the_operators_own_host_overwrites_in_place(
        self, db: Database
    ) -> None:
        def scrub(text: str) -> str:
            return text.replace("myhost.corp", "[REDACTED:hostname]")

        for _ in range(2):
            ingest_content(
                InlineIngest("indexable body of myhost.corp", content_scrubber=scrub),
                "https://myhost.corp/page",
                Progress(None),
                IngestContext(db, Settings(), overwrite=True, collection=_COLLECTION),
            )
        assert _names(db) == {self._SCRUBBED}

    def test_the_skip_is_logged_at_debug_with_the_error_class(
        self, db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="quarry.ingestion.legacy_url_names"):
            LegacyUrlNames(db).aliases(self._SCRUBBED, _COLLECTION)
        assert any("ValueError" in r.getMessage() for r in caplog.records)
        assert "REDACTED" not in caplog.text


class TestOneListingPerHost:
    """A bulk run lists a host's stored names once, not once per page."""

    def test_a_crawl_of_many_pages_from_one_host_lists_it_once(
        self, db: Database
    ) -> None:
        from unittest.mock import patch

        from quarry.db.name_prefix import NamePrefix
        from quarry.http_url import HttpUrl
        from quarry.ingestion.bulk_ingest import BulkIngestRunner, BulkOptions

        real = NamePrefix.names
        context = IngestContext(db, Settings(), collection=_COLLECTION)

        def ingest_one(
            request: UrlIngest, progress: Progress, ctx: IngestContext
        ) -> IngestResult:
            return ingest_content(
                InlineIngest("indexable body text for a crawled page"),
                request.url.document_name,
                progress,
                ctx,
            )

        pages: list[tuple[HttpUrl, str | None]] = [
            (HttpUrl(f"https://example.com/p{n}"), None) for n in range(8)
        ]
        with patch.object(NamePrefix, "names", autospec=True, side_effect=real) as spy:
            BulkIngestRunner(ingest_one=ingest_one, workers=4).run(
                pages, Progress(None), context, BulkOptions(workers=4)
            )
        assert spy.call_count == 1
        assert len(_names(db)) == 8

    def test_a_single_ingest_lists_its_host_once_too(self, db: Database) -> None:
        from unittest.mock import patch

        from quarry.db.name_prefix import NamePrefix

        real = NamePrefix.names
        with patch.object(NamePrefix, "names", autospec=True, side_effect=real) as spy:
            _store(db, "https://example.com/page")
        assert spy.call_count == 1

    def test_a_listing_is_not_shared_between_collections_or_hosts(
        self, db: Database
    ) -> None:
        from unittest.mock import patch

        from quarry.db.name_prefix import NamePrefix

        real = NamePrefix.names
        names = LegacyUrlNames(db)
        with patch.object(NamePrefix, "names", autospec=True, side_effect=real) as spy:
            names.aliases("https://example.com/a", _COLLECTION)
            names.aliases("https://example.com/b", _COLLECTION)
            names.aliases("https://example.com/a", "elsewhere")
            names.aliases("https://other.test/a", _COLLECTION)
        assert spy.call_count == 3

    def test_a_replaced_legacy_name_is_forgotten_by_the_listing(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        names = LegacyUrlNames(db)
        assert names.aliases("https://example.com/caf%C3%A9", _COLLECTION)
        names.dropped(["https://example.com/café"], _COLLECTION)
        assert names.aliases("https://example.com/caf%C3%A9", _COLLECTION) == []

    def test_a_stored_name_is_added_to_a_listed_host_only(self, db: Database) -> None:
        names = LegacyUrlNames(db)
        names.stored("https://example.com/new", _COLLECTION)
        assert names.aliases("https://example.com/new", _COLLECTION) == []


class TestEveryDedupPathAgrees:
    """What "already stored" means is decided in one place: the legacy-aware one."""

    @staticmethod
    def _job(name: str) -> ScrubbedIngestJob:
        return ScrubbedIngestJob(
            name=name,
            content="<html><body><p>captured text</p></body></html>",
            labels=IngestLabels(
                overwrite=False,
                collection=_COLLECTION,
                agent_handle="",
                memory_type="",
                summary="",
            ),
            format_hint="html",
            scrub_label="web-fetch",
        )

    def test_a_create_if_absent_capture_skips_a_legacy_named_document(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        result = self._job("https://example.com/caf%C3%A9").scrub_and_ingest(ctx)
        assert result["skipped"] == "exists"
        assert _names(db) == {"https://example.com/café"}

    def test_a_create_if_absent_capture_of_a_new_page_stores_it(
        self, db: Database
    ) -> None:
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        with patched_embedder(FakeEmbeddingBackend(768)):
            result = self._job("https://example.com/new").scrub_and_ingest(ctx)
        assert result["chunks"]

    def test_the_lookup_names_the_document_as_it_is_stored(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        names = LegacyUrlNames(db)
        stored = names.stored_as("https://example.com/caf%C3%A9", _COLLECTION)
        assert stored == "https://example.com/café"
        assert db.catalog.document_exists(stored, _COLLECTION)

    def test_a_name_stored_as_itself_is_returned_as_it_is(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page")
        names = LegacyUrlNames(db)
        assert names.stored_as("https://example.com/page", _COLLECTION) == (
            "https://example.com/page"
        )

    def test_a_name_that_is_not_stored_is_none(self, db: Database) -> None:
        assert (
            LegacyUrlNames(db).stored_as("https://example.com/x", _COLLECTION) is None
        )
