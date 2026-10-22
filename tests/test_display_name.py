"""Tests for quarry.ingestion.display_name: a document name safe for a log line."""

from __future__ import annotations

import pytest

from quarry.ingestion.display_name import DisplayName


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("https://u:pw@x.test/p?token=abc#f", "https://x.test/p"),
        ("HTTP://x.test/a?email=a@b.c", "http://x.test/a"),
        ("notes #3 (draft)?.md", "notes #3 (draft)?.md"),
        ("a://[::1", "a://[::1"),
        ("https://[::1", "(unparseable URL)"),
        ("http://u:secret@h:99999/a?token=t", "(unparseable URL)"),
    ],
    ids=[
        "secrets-dropped",
        "scheme-any-case",
        "plain-name-intact",
        "no-scheme",
        "broken",
        "bad-port-with-secrets",
    ],
)
def test_redacted(name: str, shown: str) -> None:
    assert DisplayName(name).redacted() == shown
