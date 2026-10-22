"""Tests for quarry.ingestion.fetched_body: the media-type allow-list and body."""

from __future__ import annotations

import pytest

from quarry.content_guard import UnindexableContentError, UnsupportedContentError
from quarry.ingestion.fetched_body import FetchedBody, FetchGate, MediaTypePolicy


class TestMediaTypePolicy:
    @pytest.mark.parametrize(
        "media_type",
        [
            "",
            "text/html",
            "text/plain",
            "text/markdown",
            "application/json",
            "application/xml",
            "application/xhtml+xml",
            "application/yaml",
            "application/x-yaml",
            "application/javascript",
            "application/vnd.api+json",
            "application/atom+xml",
        ],
    )
    def test_allowed(self, media_type: str) -> None:
        policy = MediaTypePolicy()
        assert policy.allows(media_type)
        policy.require(media_type)

    @pytest.mark.parametrize(
        "media_type",
        [
            "application/pdf",
            "application/octet-stream",
            "application/zip",
            "image/png",
            "audio/mpeg",
            "video/mp4",
            "font/woff2",
        ],
    )
    def test_rejected(self, media_type: str) -> None:
        with pytest.raises(UnsupportedContentError):
            MediaTypePolicy().require(media_type)

    def test_message_has_the_stable_prefix_and_no_url(self) -> None:
        with pytest.raises(UnsupportedContentError) as info:
            MediaTypePolicy().require("application/pdf")
        assert (
            str(info.value)
            == "content rejected: unsupported media type application/pdf"
        )

    def test_hostile_media_type_is_sanitized_in_the_message(self) -> None:
        with pytest.raises(UnsupportedContentError) as info:
            MediaTypePolicy().require("image/x\n<!-- injected -->")
        assert "\n" not in str(info.value)
        assert "<!--" not in str(info.value)

    def test_unsupported_content_is_an_unindexable_value_error(self) -> None:
        assert issubclass(UnsupportedContentError, UnindexableContentError)
        assert issubclass(UnsupportedContentError, ValueError)

    def test_policy_is_configurable(self) -> None:
        policy = MediaTypePolicy(exact=frozenset(), prefixes=("text/",), suffixes=())
        assert not policy.allows("application/json")
        assert policy.allows("text/plain")


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
    assert MediaTypePolicy.sanitize(raw) == expected


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
    cleaned = MediaTypePolicy.sanitize(raw)
    assert "<!--" not in cleaned, f"leaked <!-- opener from {raw!r} → {cleaned!r}"
    assert "-->" not in cleaned, f"leaked --> closer from {raw!r} → {cleaned!r}"
    assert "\n" not in cleaned and "\r" not in cleaned, (
        f"newline survived from {raw!r} → {cleaned!r}"
    )


def test_sanitize_media_type_caps_length_at_128() -> None:
    """A pathological megabyte Content-Type header is bounded at 128 chars."""
    assert MediaTypePolicy.sanitize("a" * 500) == "a" * 128


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\t\n\r", "!!!\x00\x01", "<>&{}"],
    ids=["empty", "spaces", "control", "punctuation-and-null", "unsafe-only"],
)
def test_sanitize_media_type_falls_back_when_input_degrades_to_nothing(
    raw: str,
) -> None:
    """Input that cleans to empty resolves to the RFC 2046 default octet-stream."""
    assert MediaTypePolicy.sanitize(raw) == "application/octet-stream"


class TestFetchedBody:
    @pytest.mark.parametrize(
        ("media_type", "is_html"),
        [
            ("", True),
            ("text/html", True),
            ("application/xhtml+xml", True),
            ("text/plain", False),
            ("application/json", False),
        ],
    )
    def test_is_html(self, media_type: str, *, is_html: bool) -> None:
        assert FetchedBody(text="x", media_type=media_type).is_html is is_html


class TestFetchGate:
    @pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "x"])
    def test_non_http_urls_are_refused(self, url: str) -> None:
        with pytest.raises(ValueError, match="Only HTTP"):
            FetchGate.check_url(url)

    def test_unsafe_initial_urls_are_refused(self) -> None:
        with pytest.raises(ValueError, match="URL rejected"):
            FetchGate.check_url("http://127.0.0.1/admin")

    def test_unsafe_final_urls_are_refused(self) -> None:
        with pytest.raises(ValueError, match="final URL rejected"):
            FetchGate.check_final_url("http://169.254.169.254/latest")

    def test_media_types_follow_the_policy(self) -> None:
        gate = FetchGate(MediaTypePolicy(exact=frozenset(), suffixes=()))
        gate.check_media_type("text/plain")
        with pytest.raises(UnsupportedContentError):
            gate.check_media_type("application/json")
