"""Tests for quarry.daemon.ingest_jobs — the spool_record drain-recovery contract."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

from quarry.daemon.ingest_jobs import CaptureIngestJob, IngestJob, ScrubbedIngestJob
from quarry.ingestion.web_fetch import FetchedBody

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext


def _remember(content: str, *, collection: str = "mem") -> ScrubbedIngestJob:
    return ScrubbedIngestJob(
        name="note",
        content=content,
        collection=collection,
        format_hint="auto",
        overwrite=True,
        scrub_label="remember",
        agent_handle="",
        memory_type="",
        summary="",
    )


def test_remember_spool_record_scrubs_its_content() -> None:
    """A remember snapshot is scrubbed, so a spooled file holds no raw secrets."""
    record = _remember("reach me at user@example.com").spool_record()
    assert record is not None
    assert record.kind == "remember"
    assert record.collection == "mem"
    assert "user@example.com" not in record.payload
    assert "[REDACTED:email]" in record.payload


def test_ingest_spool_record_carries_the_source_url() -> None:
    """An ingest snapshot is the source URL — the recoverable retry unit."""
    job = IngestJob(
        source="https://x.test/a",
        overwrite=False,
        collection="x.test",
        scrub=False,
        agent_handle="",
        memory_type="",
        summary="",
    )
    record = job.spool_record()
    assert record is not None
    assert record.kind == "ingest"
    assert record.payload == "https://x.test/a"


def test_capture_spool_record_is_none() -> None:
    """A capture has a durable transcript .md, so it is not spooled."""
    inline = _remember("c", collection="repo-captures")
    job = CaptureIngestJob(inline=inline, source_url="")
    assert job.spool_record() is None


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
