"""Tests for quarry.daemon.ingest_jobs — the URL job: spool record and rejections."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from unittest.mock import patch

from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.tasks import TaskState
from quarry.http_url import HttpUrl
from quarry.ingestion.fetched_body import FetchedBody
from quarry.ingestion.media_type_policy import UnsupportedContentError
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_ingest_spool_record_carries_the_source_url() -> None:
    """An ingest snapshot is the source URL — the recoverable retry unit."""
    job = IngestJob(
        source=HttpUrl("https://x.test/a"),
        scrub=False,
        labels=IngestLabels(
            overwrite=False,
            collection="x.test",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )
    record = job.spool_record()
    assert record is not None
    assert record.kind == "ingest"
    assert record.payload == "https://x.test/a"


_PDF_URL = "https://example.com/paper.pdf?token=abc"


def _scrubbed_job(url: str) -> IngestJob:
    return IngestJob(
        source=HttpUrl(url),
        scrub=True,
        labels=IngestLabels(
            overwrite=True,
            collection="repo-captures",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )


def _plain_job() -> IngestJob:
    return IngestJob(
        source=HttpUrl("https://example.com/"),
        scrub=False,
        labels=IngestLabels(
            overwrite=False,
            collection="example.com",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )


async def test_plain_ingest_of_a_refused_url_fails_the_task_without_a_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-3", kind="ingest")
    job = IngestJob(
        source=HttpUrl(_PDF_URL),
        scrub=False,
        labels=IngestLabels(
            overwrite=False,
            collection="example.com",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )
    refusal = UnsupportedContentError("content rejected: unsupported media type x")

    with (
        caplog.at_level(logging.WARNING),
        patch("quarry.ingestion.sitemap_ingest.ingest_auto", side_effect=refusal),
    ):
        await job.run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error == "content rejected: unsupported media type x"
    [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.exc_info is None
    assert record.getMessage() == (
        "Background ingest https://example.com/…(1 segments) (task t-3) rejected: "
        "content rejected: unsupported media type x"
    )


async def test_a_stored_url_completes_the_task_with_its_results(
    tmp_path: Path,
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-4", kind="ingest")

    with patch(
        "quarry.ingestion.sitemap_ingest.ingest_auto", return_value={"chunks": 7}
    ):
        await _plain_job().run(daemon.ctx, state)

    assert state.status == "completed"
    assert state.results == {"chunks": 7}
    assert state.error == ""


async def test_a_binary_body_behind_a_text_media_type_stores_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A PDF served as text/plain passes the allow-list but not the classifier."""
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-5", kind="ingest")
    body = FetchedBody(text="%PDF-1.5\n" + "�" * 4000, media_type="text/plain")

    with (
        caplog.at_level(logging.WARNING),
        patch("quarry.ingestion.web_fetch.WebFetcher.fetch_body", return_value=body),
    ):
        await _scrubbed_job("https://example.com/paper").run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error == "binary content rejected: pdf header"
    assert daemon.ctx.database.store.count() == 0
    [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.exc_info is None
