"""Every URL job ends a refused or failed fetch alike: failed, quiet, URL-free."""

from __future__ import annotations

import logging
from http.client import HTTPMessage, HTTPResponse
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.content_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.tasks import TaskState
from quarry.http_url import HttpUrl
from quarry.ingestion.fetched_body import FetchedBody
from quarry.ingestion.ssrf_redirect import RedirectRejectedError
from tests.inproc_daemon import InProcessDaemon

if TYPE_CHECKING:
    from pathlib import Path

_URL = "http://example.com/doc?token=SECRET"
_OPEN = "quarry.ingestion.opened_response.GUARDED_OPENER.open"
_DISCOVER = "quarry.sitemap.SitemapDiscovery.discover_pages"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "quarry.url_safety.socket_module.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )


def _response(content_type: str, body: bytes = b"") -> MagicMock:
    headers = HTTPMessage()
    if content_type:
        headers["Content-Type"] = content_type
    resp = MagicMock(spec=HTTPResponse)
    resp.headers = headers
    resp.url = "https://example.com/doc"
    resp.read.side_effect = [body, b""]
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _ingest_job(*, scrub: bool) -> IngestJob:
    return IngestJob(
        source=HttpUrl(_URL),
        scrub=scrub,
        labels=IngestLabels(
            overwrite=False,
            collection="repo-captures" if scrub else "example.com",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )


def _capture_job(content: str = "") -> CaptureIngestJob:
    inline = ScrubbedIngestJob(
        name="WebFetch page",
        content=content,
        format_hint="auto",
        scrub_label="capture",
        labels=IngestLabels(
            overwrite=True,
            collection="repo-captures",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )
    return CaptureIngestJob(inline=inline, source_url=HttpUrl(_URL))


def _assert_quiet_and_url_free(
    state: TaskState,
    caplog: pytest.LogCaptureFixture,
    subject: str = "ingest http://example.com/…(1 segment) (task ",
) -> None:
    assert state.status == "failed"
    for leak in ("SECRET", "u:p", "token"):
        assert leak not in state.error
        assert leak not in caplog.text
    refusals = [r for r in caplog.records if r.name.endswith("refusal_aware_terminal")]
    assert [r.levelno for r in refusals] == [logging.WARNING]
    assert refusals[0].exc_info is None
    # the URL appears only in its redacted form, once, with the task id
    assert subject in refusals[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_plain_url_ingest_of_a_pdf_fails_end_to_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-plain", kind="ingest")
    resp = _response("application/pdf", b"%PDF-1.5")

    with (
        caplog.at_level(logging.WARNING),
        patch(_DISCOVER, return_value=[]),
        patch(_OPEN, return_value=resp),
    ):
        await _ingest_job(scrub=False).run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog)
    assert state.error == "content rejected: unsupported media type application/pdf"
    assert daemon.ctx.database.store.count() == 0
    resp.read.assert_not_called()


@pytest.mark.parametrize("scrub", [False, True])
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (
            URLError("Cannot reach http://u:p@h/?token=x"),
            "fetch failed: cannot reach the host (str)",
        ),
        (HTTPError(_URL, 500, "boom", HTTPMessage(), None), "fetch failed: HTTP 500"),
        (TimeoutError("slow"), "fetch failed: timed out"),
        (
            ConnectionResetError("reset by http://u:p@h/?token=x"),
            "fetch failed: ConnectionResetError",
        ),
    ],
    ids=["unreachable", "http-500", "timeout", "reset"],
)
async def test_a_failed_fetch_fails_the_task_with_a_url_free_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: BaseException,
    reason: str,
    *,
    scrub: bool,
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-fail", kind="ingest")

    with (
        caplog.at_level(logging.WARNING),
        patch(_DISCOVER, return_value=[]),
        patch(_OPEN, side_effect=failure),
    ):
        await _ingest_job(scrub=scrub).run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog)
    assert state.error == reason
    assert daemon.ctx.database.store.count() == 0


async def test_a_capture_refetch_that_cannot_reach_the_host_fails_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-cap3", kind="capture")
    failure = URLError("Cannot reach http://u:p@h/?token=x")

    with caplog.at_level(logging.WARNING), patch(_OPEN, side_effect=failure):
        await _capture_job().run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog, subject="capture WebFetch page (task ")
    assert state.error == "fetch failed: cannot reach the host (str)"


async def test_a_url_resolving_to_a_private_address_fails_the_task(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "quarry.url_safety.socket_module.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))],
    )
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-ssrf", kind="ingest")

    with (
        caplog.at_level(logging.WARNING),
        patch(_DISCOVER, return_value=[]),
        patch(_OPEN) as opener,
    ):
        await _ingest_job(scrub=True).run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog)
    assert state.error == (
        "URL rejected: host 'example.com' resolves to blocked address 10.0.0.5"
    )
    opener.assert_not_called()


async def test_a_rejected_redirect_hop_fails_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-redir", kind="ingest")
    rejection = RedirectRejectedError("redirect target rejected: private address")

    with (
        caplog.at_level(logging.WARNING),
        patch(_DISCOVER, return_value=[]),
        patch(_OPEN, side_effect=rejection),
    ):
        await _ingest_job(scrub=True).run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog)
    assert state.error == "redirect target rejected: private address"


async def test_a_refused_capture_refetch_fails_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The inline phase stored nothing, so a refused re-fetch must not complete."""
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-cap", kind="capture")

    with (
        caplog.at_level(logging.WARNING),
        patch(_OPEN, return_value=_response("application/pdf", b"%PDF")),
    ):
        await _capture_job().run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog, subject="capture WebFetch page (task ")
    assert state.error == "content rejected: unsupported media type application/pdf"
    assert daemon.ctx.database.store.count() == 0


async def test_an_oversized_body_is_a_content_refusal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-cap2", kind="ingest")
    big = _response("text/plain")
    big.read.side_effect = [b"x" * (5 * 1024 * 1024), b""]

    with caplog.at_level(logging.WARNING), patch(_OPEN, return_value=big):
        await _ingest_job(scrub=True).run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog)
    assert state.error == "content rejected: response exceeds 4194304-byte cap"


async def test_an_unknown_charset_is_a_content_refusal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-cs", kind="ingest")
    resp = _response("text/plain; charset=nonsense-9", b"hello")

    with caplog.at_level(logging.WARNING), patch(_OPEN, return_value=resp):
        await _ingest_job(scrub=True).run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog)
    assert state.error == "content rejected: unsupported charset nonsense-9"


async def test_an_empty_capture_fails_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the inline phase nor the re-fetch extracted text: nothing is stored."""
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-empty", kind="capture")
    body = FetchedBody("<html><body></body></html>", "text/html")

    with (
        caplog.at_level(logging.WARNING),
        patch("quarry.ingestion.web_fetch.WebFetcher.fetch_body", return_value=body),
    ):
        await _capture_job().run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error == "capture stored nothing: no text extracted"
    assert daemon.ctx.database.store.count() == 0


def test_a_sitemap_with_one_pdf_page_ingests_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused page is one recorded failure; the crawl indexes the others."""
    from quarry.ingestion.bulk_ingest import BulkOptions
    from quarry.ingestion.ingest_context import IngestContext, Progress
    from quarry.ingestion.sitemap_ingest import SitemapCrawl, SitemapIngest
    from quarry.sitemap import SitemapEntry

    daemon = InProcessDaemon(tmp_path)
    entries = [
        SitemapEntry("https://good.test/a", None),
        SitemapEntry("https://good.test/paper.pdf?token=SECRET", None),
    ]

    def opener(request: object, timeout: float | None = None) -> MagicMock:
        if "paper.pdf" in getattr(request, "full_url", ""):
            return _response("application/pdf", b"%PDF")
        return _response(
            "text/html", b"<html><body><h1>Hi</h1><p>text</p></body></html>"
        )

    context = IngestContext(daemon.ctx.database, daemon.ctx.settings, collection="docs")
    with caplog.at_level(logging.WARNING), patch(_OPEN, side_effect=opener):
        result = SitemapIngest._bulk_ingest_entries(
            SitemapCrawl(
                entries=entries, source_url=HttpUrl("https://good.test/sitemap.xml")
            ),
            Progress(None),
            context,
            BulkOptions(workers=1),
        )

    assert (result["ingested"], result["failed"]) == (1, 1)
    pdf_reason = "content rejected: unsupported media type application/pdf"
    assert result["errors"] == [f"https://good.test/…(1 segment): {pdf_reason}"]
    for leak in ("SECRET", "pw@", "token"):
        assert leak not in caplog.text
        assert leak not in result["errors"][0]
    [page_warning] = [r for r in caplog.records if r.name.endswith("page_report")]
    assert page_warning.levelno == logging.WARNING
    assert page_warning.getMessage() == (
        f"Failed to ingest https://good.test/…(1 segment) ({pdf_reason})"
    )


_SITEMAP_URL = "https://example.com/sitemap.xml?token=SECRET"
_SITEMAP_SUBJECT = "ingest https://example.com/…(1 segment) (task "
_FETCHER = "usp.fetch_parse.SitemapFetcher"


def _sitemap_job(source: str = _SITEMAP_URL) -> IngestJob:
    return IngestJob(
        source=HttpUrl(source),
        scrub=False,
        labels=IngestLabels(
            overwrite=False,
            collection="example.com",
            agent_handle="",
            memory_type="",
            summary="",
        ),
    )


async def test_an_unreadable_sitemap_fails_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from usp.objects.sitemap import InvalidSitemap

    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-bad", kind="ingest")
    invalid = InvalidSitemap(url=_SITEMAP_URL, reason=f"HTTP 500 for {_SITEMAP_URL}")
    caplog.clear()  # usp logged the URL while the test built its fixture

    with caplog.at_level(logging.WARNING), patch(_FETCHER) as fetcher:
        fetcher.return_value.sitemap.return_value = invalid
        await _sitemap_job().run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog, subject=_SITEMAP_SUBJECT)
    assert state.error == "fetch failed: sitemap could not be fetched or parsed"


async def test_a_sitemap_listing_only_unsafe_urls_fails_the_task(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from usp.objects.page import SitemapPage
    from usp.objects.sitemap import PagesXMLSitemap

    monkeypatch.setattr(
        "quarry.url_safety.socket_module.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))],
    )
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-unsafe", kind="ingest")
    sitemap = PagesXMLSitemap(
        url=_SITEMAP_URL, pages=[SitemapPage(url="https://internal.test/a")]
    )

    with caplog.at_level(logging.WARNING), patch(_FETCHER) as fetcher:
        fetcher.return_value.sitemap.return_value = sitemap
        await _sitemap_job().run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog, subject=_SITEMAP_SUBJECT)
    assert state.error == "fetch failed: every URL the sitemap lists was rejected"


async def test_a_crawl_where_every_page_is_a_pdf_fails_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from usp.objects.page import SitemapPage
    from usp.objects.sitemap import PagesXMLSitemap

    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-pdfs", kind="ingest")
    sitemap = PagesXMLSitemap(
        url=_SITEMAP_URL,
        pages=[
            SitemapPage(url="https://a.test/one.pdf?token=SECRET"),
            SitemapPage(url="https://b.test/two.pdf"),
        ],
    )
    reason = "content rejected: unsupported media type application/pdf"

    with (
        caplog.at_level(logging.INFO),
        patch(_FETCHER) as fetcher,
        patch(_OPEN, return_value=_response("application/pdf", b"%PDF")),
    ):
        fetcher.return_value.sitemap.return_value = sitemap
        await _sitemap_job().run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error == f"crawl ingested nothing: 2 pages failed (first: {reason})"
    assert "SECRET" not in caplog.text
    reports = sorted(
        r.getMessage()
        for r in caplog.records
        if r.name.endswith("page_report") and r.levelno >= logging.WARNING
    )
    assert reports == [
        f"Failed to ingest https://a.test/…(1 segment) ({reason})",
        f"Failed to ingest https://b.test/…(1 segment) ({reason})",
    ]
    terminal = [r for r in caplog.records if r.name.endswith("refusal_aware_terminal")]
    assert [r.levelno for r in terminal] == [logging.WARNING]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_refetched_empty_text_body_fails_the_capture(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A marker-only body is no text: nothing meaningful would be stored."""
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-blank", kind="capture")
    body = FetchedBody("  \n", "text/plain")

    with (
        caplog.at_level(logging.WARNING),
        patch("quarry.ingestion.web_fetch.WebFetcher.fetch_body", return_value=body),
    ):
        await _capture_job().run(daemon.ctx, state)

    assert state.status == "failed"
    assert state.error == "capture stored nothing: no text extracted"
    assert daemon.ctx.database.store.count() == 0


async def test_a_scrubbed_ingest_of_an_empty_page_fails_the_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = InProcessDaemon(tmp_path)
    state = TaskState(task_id="t-empty-ingest", kind="ingest")
    body = FetchedBody("<html><body></body></html>", "text/html")

    with (
        caplog.at_level(logging.WARNING),
        patch("quarry.ingestion.web_fetch.WebFetcher.fetch_body", return_value=body),
    ):
        await _ingest_job(scrub=True).run(daemon.ctx, state)

    _assert_quiet_and_url_free(state, caplog)
    assert state.error == "capture stored nothing: no text extracted"
