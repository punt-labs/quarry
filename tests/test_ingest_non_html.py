"""Non-HTML capture through the daemon's content-routing boundary.

An empty capture payload triggers a re-fetch. The fetched Content-Type must
choose the matching loader so binary documents are not treated as HTML.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.ingest_labels import IngestLabels
from quarry.http_url import HttpUrl
from quarry.ingestion.fetched_body import FetchedBody

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
    """A non-HTML URL routes through ingest_content with a MIME marker.

    HTML uses the extractor; other media types use ``ingest_content`` with a
    leading ``<!-- media_type: X -->`` marker, matching capture re-fetches.
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
    content_arg = call.args[0].content
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
    :meth:`CapturedBodyIngest.ingest` must do the same for parity.
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


def test_captured_body_ingest_sanitizes_media_type_in_marker() -> None:
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

    content_arg = mock_ingest_content.call_args.args[0].content
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
