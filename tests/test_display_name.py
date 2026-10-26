"""Tests for quarry.display_name: a document name safe to log or store."""

from __future__ import annotations

import pytest

from quarry.display_name import DisplayName
from quarry.http_url import HttpUrl
from tests.url_names import with_query

_RESET_PATH = "/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0"


@pytest.mark.parametrize(
    ("name", "stored"),
    [
        # A name with userinfo is not an admissible URL: it is shown tolerantly,
        # with the userinfo and the whole query dropped.
        ("https://u:pw@x.test/p?token=abc#f", "https://x.test/p"),
        ("https://x.test/p;jsessionid=PSEC?t=1", with_query("https://x.test/p", "t=1")),
        ("https://x.test:443/a/;p=1", "https://x.test/a/"),
        ("HTTP://x.test/a?email=a@b.c", with_query("http://x.test/a", "email=a@b.c")),
        ("  \t https://u:SECRET@x.test/p?t=1", "https://x.test/p"),
        ("\x01\x1fhttps://x.test/p?t=1", with_query("https://x.test/p", "t=1")),
        (
            f"https://x.test{_RESET_PATH}?t=1",
            with_query(f"https://x.test{_RESET_PATH}", "t=1"),
        ),
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
    assert DisplayName(name).stored(url_sourced=True) == stored


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("https://x.test/p?token=abc", "https://x.test/…(segments=1)"),
        (f"https://x.test{_RESET_PATH}?t=1", "https://x.test/…(segments=2)"),
        ("  https://x.test/p?t=1", "https://x.test/…(segments=1)"),
        ("https://u:secret@x.test/p", "https://x.test/…(segments=1)"),
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
    url = "https://x.test:443/docs/a;jsessionid=PSEC?t=1#f"
    assert DisplayName(url).stored(url_sourced=True) == HttpUrl(url).document_name
    assert "PSEC" not in DisplayName(url).stored(url_sourced=True)


def test_a_url_that_cannot_be_admitted_never_shows_its_path() -> None:
    name = "http://u:secret@h.test/reset/PSEC?t=1"
    assert DisplayName(name).redacted() == "http://h.test/…(segments=2)"


def test_a_label_is_cleaned_but_its_typed_marker_is_not_read_as_a_digest() -> None:
    label = "https://example.com/p?q[sha256]=0123456789abcdef"
    assert DisplayName(label).stored(url_sourced=False) == HttpUrl(label).document_name
    assert DisplayName(label).stored(url_sourced=False) != label


def test_a_label_still_loses_its_userinfo_and_query_secrets() -> None:
    label = "https://u:USEC@example.com/notes?token=QSEC#FSEC"
    stored = DisplayName(label).stored(url_sourced=False)
    assert not any(secret in stored for secret in ("USEC", "QSEC", "FSEC"))
