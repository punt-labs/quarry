"""Tests for quarry.log_form: the shape of a URL in a log, whatever its host holds."""

from __future__ import annotations

import pytest

from quarry.http_url import HttpUrl
from quarry.log_form import LogForm


def test_the_form_is_scheme_netloc_and_a_segment_count() -> None:
    assert LogForm.of("https", "h.test:8443", "/a/b/") == (
        "https://h.test:8443/…(segments=2)"
    )


@pytest.mark.parametrize(
    ("char", "escaped"),
    [
        ("\x00", "\\x00"),
        ("\x0b", "\\x0b"),
        ("\u2028", "\\u2028"),
        ("\u202e", "\\u202e"),
        ("\u200f", "\\u200f"),
        ("\u061c", "\\u061c"),
    ],
)
def test_a_control_or_format_character_in_the_host_is_escaped(
    char: str, escaped: str
) -> None:
    shown = LogForm.of("http", f"exa{char}mple.com", "/p")
    assert shown == f"http://exa{escaped}mple.com/…(segments=1)"


@pytest.mark.parametrize("char", ["\x00", "\u202e", "\u2028", "\u200f"])
def test_the_tolerant_fallback_never_shows_a_raw_control(char: str) -> None:
    shown = HttpUrl.shown(f"http://exa{char}mple.com/a/b")
    assert char not in shown
