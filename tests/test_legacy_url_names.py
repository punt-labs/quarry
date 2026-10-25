"""Tests for quarry.ingestion.legacy_url_names: an old-named document is replaced.

Earlier rounds changed what a stored URL name is (query, fragment and ``;params``
dropped, default ports removed, path percent-encoded).  A re-ingest must replace
the document an older build stored under the old spelling, not orphan it.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from quarry.config import Settings
from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.db import Database
from quarry.db.chunk_store import ChunkStore
from quarry.db.name_prefix import NamePrefix
from quarry.http_url import HttpUrl
from quarry.ingestion.ingest_context import IngestContext, Progress
from quarry.ingestion.legacy_url_names import LegacyUrlNames
from quarry.ingestion.streaming import DocumentStreamer
from quarry.ingestion.web_ingest import InlineIngest, UrlIngest, ingest_content
from quarry.models import Chunk
from tests.fakes import FakeEmbeddingBackend, patched_embedder
from tests.url_names import with_query

if TYPE_CHECKING:
    from collections.abc import Iterator
    from contextlib import AbstractContextManager
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


def _store_as_stored_by_an_older_build(
    db: Database, name: str, *, url_sourced: bool = True
) -> None:
    """Write a row under *name* exactly, as a build that spelled it differently did.

    A page fetched from a URL records that URL as its path; a document someone
    named with a URL (``remember``) records none.
    """
    chunk = Chunk(
        document_name=name,
        document_path=name if url_sourced else "",
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


def _store(
    db: Database, name: str, *, overwrite: bool = True, url_sourced: bool = True
) -> None:
    """Ingest a page fetched from *name*, or a document merely labelled with it."""
    body = (
        "<html><body><p>some indexable body text for the document</p></body></html>"
        if url_sourced
        else "some indexable body text for the document"
    )
    ingest_content(
        InlineIngest(
            body, format_hint="html" if url_sourced else "auto", url_sourced=url_sourced
        ),
        # A page's caller derives its stored name once; a label is stored as typed.
        HttpUrl(name).document_name if url_sourced else name,
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

    def test_a_non_overwriting_ingest_deletes_only_the_legacy_twin(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        _store(db, "https://example.com/caf%C3%A9", overwrite=False)
        assert _names(db) == {"https://example.com/caf%C3%A9"}

    def test_a_non_overwriting_ingest_keeps_the_document_already_there(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page")
        _store(db, "https://example.com/page", overwrite=False)
        assert _names(db) == {"https://example.com/page"}

    def test_a_label_that_looks_like_a_url_is_never_replaced(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(
            db, "https://example.com/café", url_sourced=False
        )
        _store(db, "https://example.com/caf%C3%A9", overwrite=True)
        assert _names(db) == {
            "https://example.com/café",
            "https://example.com/caf%C3%A9",
        }

    def test_a_label_is_not_allowed_to_replace_a_fetched_page(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        _store(db, "https://example.com/caf%C3%A9", overwrite=True, url_sourced=False)
        assert _names(db) == {
            "https://example.com/café",
            "https://example.com/caf%C3%A9",
        }

    def test_each_replaced_twin_is_logged_at_warning(
        self, db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/page?token=SECRET")
        with caplog.at_level(
            logging.WARNING, logger="quarry.ingestion.legacy_url_names"
        ):
            _store(db, "https://example.com/page?token=SECRET", overwrite=False)
        [record] = [r for r in caplog.records if "legacy" in r.getMessage()]
        assert record.levelno == logging.WARNING
        assert "SECRET" not in caplog.text


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
                InlineIngest(
                    "indexable body of myhost.corp",
                    content_scrubber=scrub,
                    url_sourced=True,
                ),
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
        from quarry.http_url import HttpUrl
        from quarry.ingestion.bulk_ingest import BulkIngestRunner, BulkOptions

        real = NamePrefix.names
        context = IngestContext(db, Settings(), collection=_COLLECTION)

        def ingest_one(
            request: UrlIngest, progress: Progress, ctx: IngestContext
        ) -> IngestResult:
            return ingest_content(
                InlineIngest(
                    "<html><body><p>indexable body text</p></body></html>",
                    format_hint="html",
                    url_sourced=True,
                ),
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
        real = NamePrefix.names
        with patch.object(NamePrefix, "names", autospec=True, side_effect=real) as spy:
            _store(db, "https://example.com/page")
        assert spy.call_count == 1

    def test_a_listing_is_not_shared_between_collections_or_hosts(
        self, db: Database
    ) -> None:
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
            url_sourced=True,
        )

    def test_a_create_if_absent_capture_skips_a_legacy_named_document(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        result = self._job("https://example.com/caf%C3%A9").scrub_and_ingest(ctx)
        assert result["skipped"] == "exists"
        assert _names(db) == {"https://example.com/café"}

    def test_a_remembered_label_is_not_skipped_for_a_legacy_named_page(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = ScrubbedIngestJob(
            name="https://example.com/caf%C3%A9",
            content="a note that happens to be named with a url",
            labels=self._job("x").labels,
            format_hint="auto",
            scrub_label="remember",
        )

        with patched_embedder(FakeEmbeddingBackend(768)):
            result = job.scrub_and_ingest(ctx)
        assert result.get("skipped") is None
        assert result["chunks"]

    def test_a_remembered_label_with_an_html_hint_is_not_a_page_either(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = ScrubbedIngestJob(
            name="https://example.com/caf%C3%A9",
            content="<p>a note with an html hint</p>",
            labels=self._job("x").labels,
            format_hint="html",
            scrub_label="remember",
        )
        with patched_embedder(FakeEmbeddingBackend(768)):
            result = job.scrub_and_ingest(ctx)
        assert result.get("skipped") is None
        assert result["chunks"]
        assert db.catalog.document_exists("https://example.com/café", _COLLECTION)

    def test_an_overwriting_remember_with_an_html_hint_keeps_the_legacy_page(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = ScrubbedIngestJob(
            name="https://example.com/caf%C3%A9",
            content="<p>a note with an html hint</p>",
            labels=replace(self._job("x").labels, overwrite=True),
            format_hint="html",
            scrub_label="remember",
        )
        with patched_embedder(FakeEmbeddingBackend(768)):
            job.scrub_and_ingest(ctx)
        assert db.catalog.document_exists("https://example.com/café", _COLLECTION)

    def test_a_remembered_label_spelling_the_digest_marker_is_not_read_as_one(
        self, db: Database
    ) -> None:
        label = "https://example.com/p?q[sha256]=0123456789abcdef"
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = ScrubbedIngestJob(
            name=label,
            content="a plain note",
            labels=self._job("x").labels,
            format_hint="plain",
            scrub_label="remember",
        )
        with patched_embedder(FakeEmbeddingBackend(768)):
            job.scrub_and_ingest(ctx)
        assert db.catalog.document_exists(HttpUrl(label).document_name, _COLLECTION)
        assert not db.catalog.document_exists(label, _COLLECTION)

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


class TestARawNameThatLooksLikeTheDigestMarker:
    """Either reading of a stored candidate may be the one that names a page."""

    def test_a_real_query_that_spells_the_marker_is_a_legacy_name_of_its_page(
        self, db: Database
    ) -> None:
        raw = "https://example.com/p?q[sha256]=D1"
        _store_as_stored_by_an_older_build(db, raw)
        name = HttpUrl(raw).document_name
        assert name != raw
        assert LegacyUrlNames(db).aliases(name, _COLLECTION) == [raw]

    def test_a_legacy_literal_sixteen_hex_query_is_the_accepted_residual(
        self, db: Database
    ) -> None:
        # An older build stored this raw query literally.  A current-build name
        # spells only itself, and this one is already in the current form, so the
        # page's digest name does not find it: the one ambiguity that is accepted
        # (a copied digest must not alias a different page).
        raw = "https://example.com/p?q[sha256]=0123456789abcdef"
        _store_as_stored_by_an_older_build(db, raw)
        digest_name = HttpUrl(raw).document_name
        assert digest_name != raw
        assert LegacyUrlNames(db).aliases(digest_name, _COLLECTION) == []

    def test_a_current_build_name_is_not_an_alias_of_another_page(
        self, db: Database
    ) -> None:
        copied = "https://example.com/p?q[sha256]=0123456789abcdef"
        _store_as_stored_by_an_older_build(db, copied)
        other_page = HttpUrl(copied).document_name
        names = LegacyUrlNames(db)
        assert other_page != copied
        assert names.aliases(other_page, _COLLECTION) == []
        assert names.stored_as(other_page, _COLLECTION) is None

    def test_a_marker_name_in_an_old_host_spelling_is_a_legacy_name_too(
        self, db: Database
    ) -> None:
        name = with_query("https://example.com/p", "page=1")
        legacy = name.replace("example.com", "Example.COM:443")
        _store_as_stored_by_an_older_build(db, legacy)
        assert LegacyUrlNames(db).aliases(name, _COLLECTION) == [legacy]


class TestAnHtmlHintedLabelIsNeverAPage:
    def test_it_is_stored_without_a_source_path(self, db: Database) -> None:
        label = "https://example.com/notes"
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = ScrubbedIngestJob(
            name=label,
            content="<p>a note with an html hint</p>",
            labels=TestEveryDedupPathAgrees._job("x").labels,
            format_hint="html",
            scrub_label="remember",
        )
        with patched_embedder(FakeEmbeddingBackend(768)):
            job.scrub_and_ingest(ctx)
        rows = db.db.open_table("chunks").search().where("chunk_index = 0").to_list()
        assert {row["document_path"] for row in rows} == {""}

    def test_it_is_not_a_candidate_for_a_page_with_the_same_host(
        self, db: Database
    ) -> None:
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = ScrubbedIngestJob(
            name="https://example.com/notes",
            content="<p>a note with an html hint</p>",
            labels=TestEveryDedupPathAgrees._job("x").labels,
            format_hint="html",
            scrub_label="remember",
        )
        with patched_embedder(FakeEmbeddingBackend(768)):
            job.scrub_and_ingest(ctx)
        assert NamePrefix(db.db).names("https://example.com", _COLLECTION) == []


class TestACreateIfAbsentLabelChecksTheNameItStores:
    @staticmethod
    def _remember(name: str) -> ScrubbedIngestJob:
        return ScrubbedIngestJob(
            name=name,
            content="a plain note",
            labels=TestEveryDedupPathAgrees._job("x").labels,
            format_hint="plain",
            scrub_label="remember",
        )

    def test_a_name_with_a_fragment_is_stored_once_and_then_skipped(
        self, db: Database
    ) -> None:
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = self._remember("https://x.test/p#frag")
        with patched_embedder(FakeEmbeddingBackend(768)):
            first = job.scrub_and_ingest(ctx)
            second = job.scrub_and_ingest(ctx)
        assert first["chunks"]
        assert second["skipped"] == "exists"
        assert _names(db) == {"https://x.test/p"}

    def test_a_query_name_is_stored_once_and_then_skipped(self, db: Database) -> None:
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = self._remember("https://x.test/p?token=SECRET")
        with patched_embedder(FakeEmbeddingBackend(768)):
            job.scrub_and_ingest(ctx)
            second = job.scrub_and_ingest(ctx)
        assert second["skipped"] == "exists"
        assert len(_names(db)) == 1


class TestTheScrubbedNameIsTheStoredName:
    def test_a_fragment_is_dropped_from_the_name_a_log_shows(self) -> None:
        job = TestACreateIfAbsentLabelChecksTheNameItStores._remember(
            "https://x.test/p#frag"
        )
        assert job.scrubbed_name() == "https://x.test/p"

    def test_the_skip_result_and_the_log_name_the_same_document(
        self, db: Database
    ) -> None:
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        job = TestACreateIfAbsentLabelChecksTheNameItStores._remember(
            "https://x.test/p#frag"
        )
        with patched_embedder(FakeEmbeddingBackend(768)):
            job.scrub_and_ingest(ctx)
            skipped = job.scrub_and_ingest(ctx)
        assert skipped["document_name"] == job.scrubbed_name()


class TestALabelOutlivesAPageOfTheSameName:
    """End to end: a remembered html label survives a non-overwriting page capture."""

    def test_the_label_is_still_stored_and_still_a_label(self, db: Database) -> None:
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        label = ScrubbedIngestJob(
            name="https://example.com/p#frag",
            content="<p>my own note about the page</p>",
            labels=TestEveryDedupPathAgrees._job("x").labels,
            format_hint="html",
            scrub_label="remember",
        )
        page = TestEveryDedupPathAgrees._job("https://example.com/p")
        with patched_embedder(FakeEmbeddingBackend(768)):
            label.scrub_and_ingest(ctx)
            result = page.scrub_and_ingest(ctx)
        assert result["skipped"] == "exists"
        assert db.catalog.document_exists("https://example.com/p", _COLLECTION)
        rows = db.db.open_table("chunks").search().where("chunk_index = 0").to_list()
        assert {row["document_path"] for row in rows} == {""}


class TestAFailedInsertLeavesTheLegacyTwin:
    @pytest.mark.parametrize("overwrite", [False, True])
    def test_the_old_copy_survives_when_the_new_one_cannot_be_stored(
        self, db: Database, *, overwrite: bool
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        with (
            patch.object(DocumentStreamer, "insert", side_effect=RuntimeError("embed")),
            pytest.raises(RuntimeError),
        ):
            _store(db, "https://example.com/caf%C3%A9", overwrite=overwrite)
        assert "https://example.com/café" in _names(db)

    def test_the_twin_goes_once_the_new_copy_is_stored(self, db: Database) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        _store(db, "https://example.com/caf%C3%A9", overwrite=True)
        assert _names(db) == {"https://example.com/caf%C3%A9"}


class TestAScrubbedWebFetchNameIsStoredAsDerived:
    """A capture is stored under the name lookup derives, placeholders and all."""

    _HTML = "<html><body><p>captured text for the page</p></body></html>"
    _PAT = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"  # a made-up token shape

    def _capture(self, db: Database, url: str) -> str:
        from quarry.web_fetch_name import WebFetchName

        name = WebFetchName.of(url)
        job = ScrubbedIngestJob(
            name=name,
            content=self._HTML,
            labels=TestEveryDedupPathAgrees._job("x").labels,
            format_hint="html",
            scrub_label="capture",
            url_sourced=True,
        )
        ctx = cast("DaemonContext", SimpleNamespace(database=db, settings=Settings()))
        with patched_embedder(FakeEmbeddingBackend(768)):
            job.scrub_and_ingest(ctx)
        return name

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/u/jdoe@example.com/profile",
            f"https://{_PAT}.example.com/page",
        ],
        ids=["scrubbed-path", "scrubbed-host"],
    )
    def test_a_capture_is_found_by_the_name_lookup_derives(
        self, db: Database, url: str
    ) -> None:
        name = self._capture(db, url)
        assert "REDACTED" in name
        assert _names(db) == {name}
        assert LegacyUrlNames(db).stored_as(name, _COLLECTION) == name

    def test_distinct_scrubbed_hosts_do_not_collide(self, db: Database) -> None:
        first = self._capture(db, f"https://{self._PAT}.example.com/a")
        second = self._capture(db, f"https://{self._PAT}.example.com/b")
        assert first != second
        assert _names(db) == {first, second}

    def test_a_second_capture_of_the_same_page_is_still_one_document(
        self, db: Database
    ) -> None:
        url = "https://example.com/u/jdoe@example.com/profile"
        self._capture(db, url)
        self._capture(db, url)
        assert len(_names(db)) == 1


class TestTwinCleanupNeverFailsAStoredPage:
    def test_a_failing_twin_delete_still_reports_the_page_stored(
        self, db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        real = ChunkStore.delete_document

        def flaky(self: ChunkStore, name: str, *a: Any, **k: Any) -> int:
            # Any: the store's own delete arguments are forwarded unchanged.
            if name == "https://example.com/café":
                msg = "delete failed"
                raise OSError(msg)
            return real(self, name, *a, **k)

        with (
            patch.object(ChunkStore, "delete_document", flaky),
            caplog.at_level(logging.ERROR),
        ):
            _store(db, "https://example.com/caf%C3%A9", overwrite=True)
        assert "https://example.com/caf%C3%A9" in _names(db)
        assert "https://example.com/café" in _names(db)  # the twin is still there
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors
        assert "re-ingest with overwrite" in errors[0].getMessage()

    def test_the_listing_cache_stays_consistent_after_a_failed_twin_delete(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        names = LegacyUrlNames(db)
        assert names.aliases("https://example.com/caf%C3%A9", _COLLECTION)
        real = ChunkStore.delete_document

        def flaky(self: ChunkStore, name: str, *a: Any, **k: Any) -> int:
            # Any: the store's own delete arguments are forwarded unchanged.
            if name == "https://example.com/café":
                msg = "delete failed"
                raise OSError(msg)
            return real(self, name, *a, **k)

        with patch.object(ChunkStore, "delete_document", flaky):
            names.drop_twins("https://example.com/caf%C3%A9", _COLLECTION)
        # The twin is still stored, so the cache must still list it.
        assert names.aliases("https://example.com/caf%C3%A9", _COLLECTION) == [
            "https://example.com/café"
        ]


class TestAFailedUrlInsertLeavesNoPartialCopy:
    def test_the_partial_write_is_removed_and_the_twin_survives(
        self, db: Database
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        real_insert = DocumentStreamer.insert

        def partial_then_fail(
            self: DocumentStreamer, chunks: list[Chunk], store: ChunkStore, name: str
        ) -> int:
            real_insert(self, chunks[:1], store, name)  # one window lands
            msg = "second window failed"
            raise RuntimeError(msg)

        new = "https://example.com/caf%C3%A9"
        body = "<p>" + "a sentence of text. " * 400 + "</p>"
        with (
            patch.object(DocumentStreamer, "insert", partial_then_fail),
            pytest.raises(RuntimeError, match="second window"),
        ):
            ingest_content(
                InlineIngest(body, format_hint="html", url_sourced=True),
                new,
                Progress(None),
                IngestContext(db, Settings(), overwrite=True, collection=_COLLECTION),
            )
        assert new not in _names(db)
        assert "https://example.com/café" in _names(db)

    def test_a_label_is_not_cleaned_up_after_a_failed_insert(
        self, db: Database
    ) -> None:
        real_insert = DocumentStreamer.insert

        def partial_then_fail(
            self: DocumentStreamer, chunks: list[Chunk], store: ChunkStore, name: str
        ) -> int:
            real_insert(self, chunks[:1], store, name)
            msg = "boom"
            raise RuntimeError(msg)

        with (
            patch.object(DocumentStreamer, "insert", partial_then_fail),
            pytest.raises(RuntimeError),
        ):
            ingest_content(
                InlineIngest("a sentence of text. " * 400),
                "my-label",
                Progress(None),
                IngestContext(db, Settings(), overwrite=True, collection=_COLLECTION),
            )
        assert "my-label" in _names(db)  # only a URL-sourced page is cleaned up


def _delete_fails_for(
    names: set[str], error: Exception
) -> AbstractContextManager[object]:
    """Make the store's delete raise *error* for a document in *names*."""
    real = ChunkStore.delete_document

    def flaky(self: ChunkStore, name: str, *args: Any, **kwargs: Any) -> int:
        # Any: the store's own delete arguments are forwarded unchanged.
        if name in names:
            raise error
        return real(self, name, *args, **kwargs)

    return patch.object(ChunkStore, "delete_document", flaky)


class TestCleanupFailuresNeverReplaceTheRealOutcome:
    @pytest.mark.parametrize(
        "error", [OSError("io"), RuntimeError("rt"), ValueError("value")]
    )
    def test_a_twin_delete_failure_is_logged_not_raised(
        self, db: Database, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        _store_as_stored_by_an_older_build(db, "https://example.com/café")
        with (
            _delete_fails_for({"https://example.com/café"}, error),
            caplog.at_level(logging.ERROR),
        ):
            _store(db, "https://example.com/caf%C3%A9", overwrite=True)
        assert "https://example.com/caf%C3%A9" in _names(db)
        assert [r for r in caplog.records if r.levelno == logging.ERROR]

    def test_a_failed_partial_cleanup_does_not_replace_the_original_error(
        self, db: Database
    ) -> None:
        new = "https://example.com/caf%C3%A9"
        real_insert = DocumentStreamer.insert

        def partial_then_fail(
            self: DocumentStreamer, chunks: list[Chunk], store: ChunkStore, name: str
        ) -> int:
            real_insert(self, chunks[:1], store, name)
            msg = "the insert failed"
            raise RuntimeError(msg)

        body = "<p>" + "a sentence of text. " * 400 + "</p>"
        with (
            patch.object(DocumentStreamer, "insert", partial_then_fail),
            _delete_fails_for({new}, ValueError("cleanup failed")),
            pytest.raises(RuntimeError, match="the insert failed"),
        ):
            ingest_content(
                InlineIngest(body, format_hint="html", url_sourced=True),
                new,
                Progress(None),
                IngestContext(db, Settings(), overwrite=False, collection=_COLLECTION),
            )

    def test_a_failed_twin_lookup_after_the_store_still_reports_the_page_stored(
        self, db: Database, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken(*_a: object, **_k: object) -> list[str]:
            msg = "listing failed"
            raise RuntimeError(msg)

        with (
            patch.object(NamePrefix, "names", broken),
            caplog.at_level(logging.ERROR),
        ):
            _store(db, "https://example.com/p", overwrite=True)
        assert "https://example.com/p" in _names(db)
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors
        assert "re-ingest with overwrite" in errors[0].getMessage()
