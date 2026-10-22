"""Tests for quarry.capture_marker: the media-type line on a captured body."""

from __future__ import annotations

import pytest

from quarry.capture_marker import CaptureMarker


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("application/json", "application/json"),
        ("text/html; charset=utf-8", "text/html;charset=utf-8"),
        ("application/vnd.api+json", "application/vnd.api+json"),
    ],
    ids=["json", "html-charset", "vendor-plus"],
)
def test_passes_well_formed_values(raw: str, expected: str) -> None:
    """A well-formed RFC 6838 media type survives sanitization unchanged."""
    assert CaptureMarker(raw).media_type == expected


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
def test_strips_marker_escape_attempts(raw: str) -> None:
    """Whitespace, control bytes, and comment-escape needles are stripped clean."""
    cleaned = CaptureMarker(raw).media_type
    assert "<!--" not in cleaned, f"leaked <!-- opener from {raw!r} → {cleaned!r}"
    assert "-->" not in cleaned, f"leaked --> closer from {raw!r} → {cleaned!r}"
    assert "\n" not in cleaned and "\r" not in cleaned, (
        f"newline survived from {raw!r} → {cleaned!r}"
    )


def test_caps_length_at_128() -> None:
    """A pathological megabyte Content-Type header is bounded at 128 chars."""
    assert CaptureMarker("a" * 500).media_type == "a" * 128


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\t\n\r", "!!!\x00\x01", "<>&{}"],
    ids=["empty", "spaces", "control", "punctuation-and-null", "unsafe-only"],
)
def test_falls_back_when_input_degrades_to_nothing(
    raw: str,
) -> None:
    """Input that cleans to empty resolves to the RFC 2046 default octet-stream."""
    assert CaptureMarker(raw).media_type == "application/octet-stream"


def test_render_is_one_line_with_trailing_newline() -> None:
    assert CaptureMarker("text/plain").render() == "<!-- media_type: text/plain -->\n"


def test_without_strips_a_leading_marker_only() -> None:
    text = CaptureMarker("text/plain").render() + "body"
    assert CaptureMarker.without(text) == "body"


def test_without_leaves_text_that_does_not_open_with_a_marker() -> None:
    text = "body\n<!-- media_type: text/plain -->\n"
    assert CaptureMarker.without(text) == text
