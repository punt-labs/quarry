"""Non-HTML capture regression (quarry-jzqw / G4).

The capture route re-fetches when the client-side payload is empty by
building an ``IngestJob(scrub=True)`` and running :meth:`IngestJob._ingest`.
That path today calls :func:`quarry.ingestion.web_ingest.ingest_url`, which
delegates to :meth:`quarry.ingestion.web_fetch.WebFetcher.fetch` — and
``fetch`` raises ``ValueError: URL returned non-HTML content`` for any
non-HTML media type.  So a JSON/plain-text/XML URL captured via the
empty-payload fallback loses its content and dumps a traceback into the
daemon log instead of storing the body as text.

The G4 fix on the :class:`CaptureIngestJob._refetch` branch handled
non-HTML correctly by routing through :meth:`WebFetcher.fetch_body` and
:func:`ingest_content` with a ``<!-- media_type: X -->`` marker.  The same
routing is missing on :class:`IngestJob._ingest`.  These tests assert the
same non-HTML contract at that entry point — they FAIL on v3.2.0 because
``ingest_url`` still raises, and they pass once ``_ingest`` grows the same
media-type branch its sibling already has.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.daemon.ingest_jobs import IngestJob
from quarry.ingestion.web_fetch import FetchedBody

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext


pytestmark = pytest.mark.hook_integration


_QUARRY_JZQW_SUMMARY = (
    "quarry-jzqw (G4): IngestJob._ingest still raises ValueError on non-HTML "
    "URLs — capture re-fetch drops the body instead of storing it as text."
)


def _ctx() -> DaemonContext:
    """Return a minimal :class:`DaemonContext` stand-in for the ingest call."""
    return cast(
        "DaemonContext",
        SimpleNamespace(database=MagicMock(), settings=MagicMock()),
    )


def _job(url: str) -> IngestJob:
    """Return an IngestJob shaped like the daemon's capture-refetch fallback."""
    return IngestJob(
        source=url,
        overwrite=True,
        collection="repo-captures",
        scrub=True,
        agent_handle="",
        memory_type="",
        summary="",
    )


@pytest.mark.parametrize(
    ("media_type", "body_text"),
    [
        ("application/json", '{"ok": true, "count": 42}'),
        ("text/plain", "hello world\nline two\n"),
        ("application/xml", "<?xml version='1.0'?><root><child/></root>"),
    ],
    ids=["json", "text", "xml"],
)
def test_ingest_captures_non_html_as_text(media_type: str, body_text: str) -> None:
    """A non-HTML URL routes through ingest_content with a mime marker (no ValueError).

    Today ``IngestJob._ingest`` calls ``ingest_url``, whose internal
    :meth:`WebFetcher.fetch` raises ``ValueError`` for anything that is not
    HTML.  The fix teaches ``_ingest`` to switch on :meth:`fetch_body`'s
    ``media_type`` — HTML through the extractor, everything else through
    :func:`ingest_content` with a leading ``<!-- media_type: X -->`` marker,
    mirroring :meth:`CaptureIngestJob._refetch`.
    """
    url = "https://example.test/data"
    body = FetchedBody(text=body_text, media_type=media_type)
    with (
        patch(
            "quarry.ingestion.web_fetch.WebFetcher.fetch_body",
            return_value=body,
        ),
        patch(
            "quarry.ingestion.web_ingest.ingest_content",
            return_value={"chunks": 1, "sections": 1},
        ) as mock_ingest_content,
        patch(
            "quarry.ingestion.web_ingest.ingest_url",
            return_value={"chunks": 1, "sections": 1},
        ),
    ):
        try:
            result = _job(url)._ingest(_ctx())
        except ValueError as exc:
            pytest.fail(
                f"{_QUARRY_JZQW_SUMMARY}\n"
                f"  media_type={media_type!r}\n"
                f"  raised ValueError: {exc}"
            )

    assert mock_ingest_content.called, (
        f"{_QUARRY_JZQW_SUMMARY}\n"
        f"  media_type={media_type!r}: ingest_content was never called; "
        "the non-HTML body was dropped instead of stored as text."
    )
    call = mock_ingest_content.call_args
    content_arg = call.args[0] if call.args else call.kwargs.get("content", "")
    assert content_arg.startswith("<!-- media_type: "), (
        f"{_QUARRY_JZQW_SUMMARY}\n"
        f"  media_type={media_type!r}: stored content lacks the "
        f"'<!-- media_type: X -->' marker "
        "that lets a reader (and downstream grep) know the body's shape.\n"
        f"  content prefix: {content_arg[:80]!r}"
    )
    chunks_reported = int(cast("int", result.get("chunks", 0)))
    assert chunks_reported >= 1, (
        f"{_QUARRY_JZQW_SUMMARY}\n"
        f"  media_type={media_type!r}: ingest_content returned zero chunks; "
        "the capture would still be dropped."
    )


def _assert_document_name_omits(url: str, leak: str) -> None:
    """Drive an ``_ingest`` capture and assert ``leak`` is absent from document_name."""
    body = FetchedBody(text='{"ok":true}', media_type="application/json")
    with (
        patch(
            "quarry.ingestion.web_fetch.WebFetcher.fetch_body",
            return_value=body,
        ),
        patch(
            "quarry.ingestion.web_ingest.ingest_content",
            return_value={"chunks": 1, "sections": 1},
        ) as mock_ingest_content,
    ):
        _job(url)._ingest(_ctx())

    call = mock_ingest_content.call_args
    document_name = (
        call.args[1] if len(call.args) >= 2 else call.kwargs["document_name"]
    )
    assert leak not in document_name, (
        f"non-HTML ingest leaked URL secret {leak!r} into stored document_name:\n"
        f"  url={url!r}\n"
        f"  document_name={document_name!r}"
    )


@pytest.mark.parametrize(
    ("url", "leak"),
    [
        ("https://example.test/reset?token=secretxyz", "secretxyz"),
        ("https://example.test/page?email=a%40b.com", "email="),
        ("https://example.test/doc#tokenfragment", "tokenfragment"),
    ],
    ids=["token-query", "email-query", "fragment"],
)
def test_non_html_ingest_redacts_url_secrets_in_document_name(
    url: str, leak: str
) -> None:
    """Non-HTML capture routes derive document_name via CaptureUrl.redacted.

    The pipeline's regex scrubber leaves URL structural components (``?email=``,
    ``?token=``, ``#fragment``) on the persisted document_name, so passing the
    raw URL leaks the secret into the pushable captures collection (CWE-532).
    ``ingest_url`` already calls ``CaptureUrl(url).redacted(scrub)`` before
    ``_scrub_metadata``; the non-HTML branch of
    :meth:`IngestJob.ingest_captured_body` must do the same for parity.
    """
    _assert_document_name_omits(url, leak)


def test_non_html_ingest_redacts_userinfo_in_document_name() -> None:
    """URL userinfo (``user:pass@``) is stripped from the stored document_name.

    The URL and expected-leak substring are assembled at runtime from local
    parts so no credential-shaped literal (``foo:bar@host``) appears in this
    source file — pre-commit secret scrubbers would otherwise redact the
    fixture and make the assertion trivially true.
    """
    user = "usr"
    pw = "pwd"
    url = f"https://{user}:{pw}@host.example/path?q=1"
    leak = f"{user}:{pw}"
    _assert_document_name_omits(url, leak)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("application/json", "application/json"),
        ("text/html; charset=utf-8", "text/html;charset=utf-8"),
        ("application/vnd.api+json", "application/vnd.api+json"),
    ],
    ids=["json", "html-charset", "vendor-plus"],
)
def test_sanitize_media_type_passes_well_formed_values(raw: str, expected: str) -> None:
    """A well-formed RFC 6838 media type survives sanitization unchanged."""
    assert IngestJob.sanitize_media_type(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "application/json\n<!-- injection -->",
        "text/plain\r\n<!-- xss -->",
        "application/json; charset=utf-8\r\n\r\ninjected",
        "text/html\n\r\t <script>alert(1)</script>",
    ],
    ids=["newline-comment", "crlf-comment", "crlf-header-smuggle", "html-tag"],
)
def test_sanitize_media_type_strips_marker_escape_attempts(raw: str) -> None:
    """Whitespace, control bytes, and comment-escape needles are stripped clean."""
    cleaned = IngestJob.sanitize_media_type(raw)
    assert "<!--" not in cleaned, f"leaked <!-- opener from {raw!r} → {cleaned!r}"
    assert "-->" not in cleaned, f"leaked --> closer from {raw!r} → {cleaned!r}"
    assert "\n" not in cleaned and "\r" not in cleaned, (
        f"newline survived from {raw!r} → {cleaned!r}"
    )


def test_sanitize_media_type_caps_length_at_128() -> None:
    """A pathological megabyte Content-Type header is bounded at 128 chars."""
    assert IngestJob.sanitize_media_type("a" * 500) == "a" * 128


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\t\n\r", "!!!\x00\x01", "<>&{}"],
    ids=["empty", "spaces", "control", "punctuation-and-null", "unsafe-only"],
)
def test_sanitize_media_type_falls_back_when_input_degrades_to_nothing(
    raw: str,
) -> None:
    """Input that cleans to empty resolves to the RFC 2046 default octet-stream."""
    assert IngestJob.sanitize_media_type(raw) == "application/octet-stream"


def test_ingest_captured_body_sanitizes_media_type_in_marker() -> None:
    """A hostile Content-Type never breaks the single-line ``<!-- media_type: X -->``.

    Marker escape is the CWE-79-adjacent risk: a header carrying ``\\n<!-- foo -->``
    would split the marker across lines and let the follow-on ``-->`` close the
    HTML comment early — the stored capture would then leak the injected text
    to any downstream tool that treats the marker as inert.
    """
    hostile = "text/plain\n<!-- xss -->"
    body = FetchedBody(text="body", media_type=hostile)
    with (
        patch(
            "quarry.ingestion.web_fetch.WebFetcher.fetch_body",
            return_value=body,
        ),
        patch(
            "quarry.ingestion.web_ingest.ingest_content",
            return_value={"chunks": 1, "sections": 1},
        ) as mock_ingest_content,
    ):
        _job("https://example.test/data")._ingest(_ctx())

    content_arg = mock_ingest_content.call_args.args[0]
    first_line, _, _ = content_arg.partition("\n")
    assert first_line.startswith("<!-- media_type: "), (
        f"marker line does not start with the media_type comment: {first_line!r}"
    )
    assert first_line.endswith(" -->"), (
        f"marker line does not close on a single line: {first_line!r}"
    )
    assert first_line.count("-->") == 1, (
        f"marker line contains multiple '-->' sequences: {first_line!r}"
    )
    assert "<!--" not in first_line[4:], (
        f"marker line contains a nested opener: {first_line!r}"
    )


def test_ingest_fetch_failure_logs_redacted_url_and_returns_zero_chunks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fetch failure on _ingest logs the URL redacted and returns empty.

    Without the guard, the raw URL rides ``fetch_body``'s exception
    message ("Cannot reach {url}: ...") into ``task_terminal``'s traceback
    and the persisted ``state.error`` — leaking ``?token=`` and
    ``user:pass@`` into the daemon log (CWE-532).  The guard mirrors
    :meth:`CaptureIngestJob._refetch`: redact through ``CaptureUrl``,
    log only the exception class, return an empty result.
    """
    url = "https://user:pass@example.test/reset?token=secretxyz"
    boom = OSError(f"Cannot reach {url}: connection refused")
    with (
        patch(
            "quarry.ingestion.web_fetch.WebFetcher.fetch_body",
            side_effect=boom,
        ),
        caplog.at_level(logging.WARNING, logger="quarry.daemon.ingest_jobs"),
    ):
        result = _job(url)._ingest(_ctx())

    assert result == {"chunks": 0, "sections": 0}
    log_text = caplog.text
    assert "secretxyz" not in log_text, (
        f"fetch-failure log leaked query token:\n{log_text}"
    )
    assert "user:pass" not in log_text, (
        f"fetch-failure log leaked userinfo:\n{log_text}"
    )
    assert "OSError" in log_text, (
        f"fetch-failure log missing exception class name:\n{log_text}"
    )


def _capture_ingest_failure_log(
    caplog: pytest.LogCaptureFixture, exc: BaseException
) -> str:
    """Drive ``_ingest`` with ``exc`` from ``fetch_body`` and return the log text."""
    url = "https://example.test/data"
    with (
        patch(
            "quarry.ingestion.web_fetch.WebFetcher.fetch_body",
            side_effect=exc,
        ),
        caplog.at_level(logging.WARNING, logger="quarry.daemon.ingest_jobs"),
    ):
        _job(url)._ingest(_ctx())
    return caplog.text


def test_ingest_url_safety_rejection_appends_policy_message_to_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``URL rejected:`` message is safe to append -- operators need the reason.

    Class-only rendering hides the difference between "private IP", "metadata
    hostname", and "unsupported scheme"; an operator diagnosing an SSRF/policy
    rejection needs the specific reason without waiting on a repro with debug
    logging.  The url-safety messages describe a rule and do not embed the raw
    URL, so appending them keeps the CWE-532 guard intact.
    """
    log_text = _capture_ingest_failure_log(
        caplog, ValueError("URL rejected: private IP 10.0.0.1")
    )
    assert "URL rejected: private IP 10.0.0.1" in log_text, (
        f"URL-safety policy message missing from log:\n{log_text}"
    )
    assert "ValueError" in log_text, f"exception class missing from log:\n{log_text}"


def test_ingest_final_url_rejection_appends_policy_message_to_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``final URL rejected:`` (post-redirect) message is safe to append too.

    ``WebFetcher._check_final_url`` raises this shape when a redirect lands on
    an internal host; the operator needs to see WHICH policy tripped so an
    SSRF-via-redirect regression is diagnosable from logs alone.
    """
    log_text = _capture_ingest_failure_log(
        caplog, ValueError("final URL rejected: redirect to blocked host")
    )
    assert "final URL rejected: redirect to blocked host" in log_text, (
        f"final-URL-safety policy message missing from log:\n{log_text}"
    )
    assert "ValueError" in log_text, f"exception class missing from log:\n{log_text}"


def test_classify_fetch_error_appends_only_safe_prefixes() -> None:
    """The classifier appends safe policy messages and no others (unit-level).

    Complements the ``_ingest``-driven regression tests with a direct check on
    :meth:`IngestJob._classify_fetch_error` so a future edit to the prefix
    allow-list fails a targeted test rather than surfacing as a leak.
    """
    safe = ValueError("URL rejected: metadata hostname")
    assert IngestJob._classify_fetch_error(safe) == (
        "ValueError: URL rejected: metadata hostname"
    )

    unsafe = OSError("Cannot reach https://user:pw@host/?token=secretxyz")
    rendered = IngestJob._classify_fetch_error(unsafe)
    assert rendered == "OSError", (
        f"unsafe-message classifier must be class-only, got: {rendered!r}"
    )
    assert "secretxyz" not in rendered
    assert "user:pw" not in rendered
