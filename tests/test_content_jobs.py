"""Tests for quarry.daemon.content_jobs — scrub, spool, create-if-absent, re-fetch."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

from quarry.daemon.content_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.ingestion.web_fetch import FetchedBody

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext


def _remember(
    content: str, *, collection: str = "mem", overwrite: bool = True
) -> ScrubbedIngestJob:
    return ScrubbedIngestJob(
        name="note",
        content=content,
        collection=collection,
        format_hint="auto",
        overwrite=overwrite,
        scrub_label="remember",
        agent_handle="",
        memory_type="",
        summary="",
    )


def _ctx(*, exists: bool) -> tuple[DaemonContext, MagicMock]:
    """Return a context whose catalog answers ``document_exists`` with *exists*."""
    catalog = MagicMock()
    catalog.document_exists.return_value = exists
    ctx = SimpleNamespace(
        database=SimpleNamespace(catalog=catalog, store=MagicMock()),
        settings=MagicMock(),
    )
    return cast("DaemonContext", ctx), catalog


def test_remember_spool_record_scrubs_its_content() -> None:
    """A remember snapshot is scrubbed, so a spooled file holds no raw secrets."""
    record = _remember("reach me at user@example.com").spool_record()
    assert record is not None
    assert record.kind == "remember"
    assert record.collection == "mem"
    assert "user@example.com" not in record.payload
    assert "[REDACTED:email]" in record.payload


class TestCreateIfAbsent:
    """``overwrite=False`` is create-if-absent, decided on the collection's writer.

    A client-side existence check reads before a racing writer's 202 has been
    indexed, so two writers that both saw "absent" would otherwise both land:
    the second either replaces the first (overwrite) or appends a second
    chunk set under one name. The check here runs inside the per-collection
    FIFO worker, after every earlier write to the collection has landed.
    """

    def test_existing_document_is_left_untouched(self) -> None:
        ctx, catalog = _ctx(exists=True)
        with patch("quarry.ingestion.web_ingest.ingest_content") as ingest:
            result = _remember("second", overwrite=False).scrub_and_ingest(ctx)
        ingest.assert_not_called()
        catalog.document_exists.assert_called_once_with("note", "mem")
        assert result == {
            "document_name": "note",
            "collection": "mem",
            "chunks": 0,
            "skipped": "exists",
        }

    def test_absent_document_is_written_without_a_delete(self) -> None:
        ctx, _catalog = _ctx(exists=False)
        with patch(
            "quarry.ingestion.web_ingest.ingest_content",
            return_value={"document_name": "note", "collection": "mem", "chunks": 1},
        ) as ingest:
            result = _remember("first", overwrite=False).scrub_and_ingest(ctx)
        assert result["chunks"] == 1
        context = ingest.call_args.args[3]
        assert context.overwrite is False

    def test_overwrite_replaces_without_consulting_the_catalog(self) -> None:
        ctx, catalog = _ctx(exists=True)
        with patch(
            "quarry.ingestion.web_ingest.ingest_content",
            return_value={"document_name": "note", "collection": "mem", "chunks": 1},
        ) as ingest:
            _remember("forced", overwrite=True).scrub_and_ingest(ctx)
        ingest.assert_called_once()
        catalog.document_exists.assert_not_called()
        assert ingest.call_args.args[3].overwrite is True

    def test_existence_is_checked_under_the_stored_scrubbed_name(self) -> None:
        """The pipeline stores the scrubbed name, so that is the name to look up."""
        ctx, catalog = _ctx(exists=True)
        job = ScrubbedIngestJob(
            name="note from user@example.com",
            content="body",
            collection="mem",
            format_hint="auto",
            overwrite=False,
            scrub_label="remember",
            agent_handle="",
            memory_type="",
            summary="",
        )
        with patch("quarry.ingestion.web_ingest.ingest_content"):
            job.scrub_and_ingest(ctx)
        looked_up = catalog.document_exists.call_args.args[0]
        assert "user@example.com" not in looked_up
        assert "[REDACTED:email]" in looked_up


def test_capture_spool_record_is_none() -> None:
    """A capture has a durable transcript .md, so it is not spooled."""
    inline = _remember("c", collection="repo-captures")
    job = CaptureIngestJob(inline=inline, source_url="")
    assert job.spool_record() is None


class TestCaptureSkipVersusEmpty:
    """A create-if-absent skip is not an empty extraction; only the latter re-fetches.

    Both answer ``chunks: 0``. The skip means the document is already stored
    under this name, so a re-fetch would spend a network round trip to write
    (or be skipped again) for a page the daemon holds; the empty extraction
    means a JS-rendered page produced nothing, and the re-fetch is the rescue.
    """

    def _job(self, source_url: str = "https://x.test/page") -> CaptureIngestJob:
        inline = _remember("<html></html>", collection="repo-captures", overwrite=False)
        return CaptureIngestJob(inline=inline, source_url=source_url)

    def test_existing_document_is_skipped_without_a_refetch(self) -> None:
        ctx, _catalog = _ctx(exists=True)
        job = self._job()
        with (
            patch("quarry.ingestion.web_ingest.ingest_content") as ingest,
            patch.object(CaptureIngestJob, "_refetch") as refetch,
        ):
            result = job._capture(ctx)
        ingest.assert_not_called()
        refetch.assert_not_called()
        assert result["skipped"] == "exists"
        assert result["chunks"] == 0

    def test_empty_extraction_with_a_source_url_refetches(self) -> None:
        ctx, _catalog = _ctx(exists=False)
        job = self._job()
        empty = {"document_name": "note", "collection": "repo-captures", "chunks": 0}
        with (
            patch("quarry.ingestion.web_ingest.ingest_content", return_value=empty),
            patch.object(
                CaptureIngestJob, "_refetch", return_value={"chunks": 2}
            ) as refetch,
        ):
            result = job._capture(ctx)
        refetch.assert_called_once_with(ctx)
        assert result == {"chunks": 2}

    def test_empty_extraction_without_a_source_url_stores_what_it_has(self) -> None:
        ctx, _catalog = _ctx(exists=False)
        job = self._job(source_url="")
        empty = {"document_name": "note", "collection": "repo-captures", "chunks": 0}
        with (
            patch("quarry.ingestion.web_ingest.ingest_content", return_value=empty),
            patch.object(CaptureIngestJob, "_refetch") as refetch,
        ):
            result = job._capture(ctx)
        refetch.assert_not_called()
        assert result == empty

    def test_non_empty_extraction_never_refetches(self) -> None:
        ctx, _catalog = _ctx(exists=False)
        job = self._job()
        stored = {"document_name": "note", "collection": "repo-captures", "chunks": 3}
        with (
            patch("quarry.ingestion.web_ingest.ingest_content", return_value=stored),
            patch.object(CaptureIngestJob, "_refetch") as refetch,
        ):
            result = job._capture(ctx)
        refetch.assert_not_called()
        assert result == stored


def test_html_refetch_fetches_the_source_url_exactly_once() -> None:
    """The HTML re-fetch branch must not fetch the same URL twice.

    ``fetch_body`` (used to decide HTML vs. text routing) and ``ingest_url``'s
    own internal fetch used to run back to back — doubling the network I/O for
    every empty-chunk HTML re-fetch. ``ingest_url`` now takes the already-
    fetched body via ``prefetched_html`` and skips its own fetch.
    """
    inline = _remember("", collection="repo-captures")
    job = CaptureIngestJob(inline=inline, source_url="https://x.test/page")
    ctx = cast(
        "DaemonContext", SimpleNamespace(database=MagicMock(), settings=MagicMock())
    )
    body = FetchedBody(
        text="<html><body><p>hi</p></body></html>", media_type="text/html"
    )

    with (
        patch(
            "quarry.ingestion.web_fetch.WebFetcher.fetch_body", return_value=body
        ) as mock_fetch_body,
        patch(
            "quarry.ingestion.web_ingest.ingest_url", return_value={"chunks": 1}
        ) as mock_ingest_url,
    ):
        result = job._refetch(ctx)

    mock_fetch_body.assert_called_once_with("https://x.test/page")
    mock_ingest_url.assert_called_once()
    request = mock_ingest_url.call_args.args[0]
    assert request.prefetched_html == body.text
    assert result == {"chunks": 1}
