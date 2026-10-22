"""Tests for quarry.display_name: a document name safe to log or store."""

from __future__ import annotations

import pytest

from quarry.display_name import DisplayName

_RESET_PATH = "/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0"


@pytest.mark.parametrize(
    ("name", "stored"),
    [
        ("https://u:pw@x.test/p?token=abc#f", "https://x.test/p"),
        ("https://x.test/p;jsessionid=PSEC?t=1", "https://x.test/p"),
        ("https://x.test:443/a/;p=1", "https://x.test/a/"),
        ("HTTP://x.test/a?email=a@b.c", "http://x.test/a"),
        ("  \t https://u:SECRET@x.test/p?t=1", "https://x.test/p"),
        ("\x01\x1fhttps://x.test/p?t=1", "https://x.test/p"),
        (f"https://x.test{_RESET_PATH}?t=1", f"https://x.test{_RESET_PATH}"),
        ("notes #3 (draft)?.md", "notes #3 (draft)?.md"),
        ("a://[::1", "a://[::1"),
        ("https://[::1", "(unparseable URL)"),
        ("http://u:secret@h:99999/a?token=t", "(unparseable URL)"),
    ],
    ids=[
        "secrets-dropped",
        "path-params-dropped",
        "default-port-and-params",
        "scheme-any-case",
        "leading-whitespace",
        "leading-control",
        "path-kept",
        "plain-name-intact",
        "no-scheme",
        "broken",
        "bad-port-with-secrets",
    ],
)
def test_stored(name: str, stored: str) -> None:
    assert DisplayName(name).stored() == stored


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("https://x.test/p?token=abc", "https://x.test/…(1 segment)"),
        (f"https://x.test{_RESET_PATH}?t=1", "https://x.test/…(2 segments)"),
        ("  https://x.test/p?t=1", "https://x.test/…(1 segment)"),
        ("https://u:secret@x.test/p", "https://x.test/…(1 segment)"),
        ("notes #3 (draft)?.md", "notes #3 (draft)?.md"),
        ("http://u:secret@h:99999/a", "(unparseable URL)"),
    ],
    ids=[
        "query",
        "token-elided",
        "leading-whitespace",
        "userinfo",
        "plain",
        "bad-port",
    ],
)
def test_redacted(name: str, shown: str) -> None:
    assert DisplayName(name).redacted() == shown


def test_a_url_named_document_is_stored_as_a_url_ingest_of_it_would_be() -> None:
    from quarry.http_url import HttpUrl

    url = "https://x.test:443/docs/a;jsessionid=PSEC?t=1#f"
    assert DisplayName(url).stored() == HttpUrl(url).document_name
    assert "PSEC" not in DisplayName(url).stored()


def test_a_url_that_cannot_be_admitted_never_shows_its_path() -> None:
    name = "http://u:secret@h.test/reset/PSEC?t=1"
    assert DisplayName(name).redacted() == "http://h.test/…(2 segments)"
