"""Tests for quarry.daemon.ingest_jobs — the URL job: spool record and rejections."""

from __future__ import annotations

import logging
from http.client import HTTPMessage, HTTPResponse
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from quarry.content_guard import UnsupportedContentError
from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.tasks import TaskState
from quarry.ingestion.fetched_body import FetchedBody
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from pathlib import Path


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


_PDF_URL = "https://user:secret@example.com/paper.pdf?token=abc"


def _scrubbed_job(url: str) -> IngestJob:
    return IngestJob(
        source=url,
        overwrite=True,
        collection="repo-captures",
        scrub=True,
        agent_handle="",
        memory_type="",
        summary="",
    )


def _plain_job() -> IngestJob:
    return IngestJob(
        source="https://example.com/",
        overwrite=False,
        collection="example.com",
        scrub=False,
        agent_handle="",
        memory_type="",
        summary="",
    )


def _pdf_response() -> MagicMock:
    """Return an open-response mock for a PDF whose body must never be read."""
    headers = HTTPMessage()
    headers["Content-Type"] = "application/pdf"
    resp = MagicMock(spec=HTTPResponse)
    resp.headers = headers
    resp.url = "https://example.com/paper.pdf"
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@pytest.fixture
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "quarry.url_safety.socket_module.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


@pytest.mark.usefixtures("_public_dns")
async def test_pdf_capture_fails_the_task_and_stores_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A PDF behind a capture URL is refused, not stored as 800 chunks of binary."""
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-1", kind="ingest")
    resp = _pdf_response()

    with (
        caplog.at_level(logging.WARNING),
        patch("quarry.ingestion.web_fetch.GUARDED_OPENER.open", return_value=resp),
    ):
        await _scrubbed_job(_PDF_URL).run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error == "content rejected: unsupported media type application/pdf"
    assert daemon.ctx.database.store.count() == 0
    resp.read.assert_not_called()


@pytest.mark.usefixtures("_public_dns")
async def test_pdf_capture_logs_one_redacted_warning_without_a_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-2", kind="ingest")

    with (
        caplog.at_level(logging.WARNING),
        patch(
            "quarry.ingestion.web_fetch.GUARDED_OPENER.open",
            return_value=_pdf_response(),
        ),
    ):
        await _scrubbed_job(_PDF_URL).run(daemon.ctx, state)

    [record] = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "secret" not in record.getMessage()
    assert "token" not in record.getMessage()
    assert "example.com/paper.pdf" in record.getMessage()


async def test_plain_ingest_of_a_refused_url_fails_the_task_without_a_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-3", kind="ingest")
    job = IngestJob(
        source=_PDF_URL,
        overwrite=False,
        collection="example.com",
        scrub=False,
        agent_handle="",
        memory_type="",
        summary="",
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
    assert "secret" not in record.getMessage()


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
