"""Tests for quarry.web_fetch_name: the stored name of a WebFetch capture."""

from __future__ import annotations

import pytest

from quarry.web_fetch_name import WebFetchName


def test_the_name_is_the_storage_form_of_the_url() -> None:
    url = "https://x.test:8443/reset?email=user@example.com#frag"
    assert WebFetchName.of(url) == "https://x.test:8443/reset"


def test_query_and_fragment_collapse_to_same_name() -> None:
    """Two URLs differing only by query string or fragment share one document."""
    base = WebFetchName.of("https://x.test/docs/guide")
    with_query = WebFetchName.of("https://x.test/docs/guide?utm=1")
    with_fragment = WebFetchName.of("https://x.test/docs/guide#section")
    assert base == with_query == with_fragment


def test_a_trailing_slash_stays_a_distinct_document() -> None:
    assert WebFetchName.of("https://x.test/docs") != WebFetchName.of(
        "https://x.test/docs/"
    )


def test_an_email_in_the_path_is_scrubbed() -> None:
    name = WebFetchName.of("https://x.test/u/user@example.com/profile")
    assert "user@example.com" not in name


@pytest.mark.parametrize(
    "url",
    [
        "http://u:SECRET@x.test/p",
        "http://x.test:99999/p",
        "http://x.test/a b",
        "ftp://x.test/p",
        "",
    ],
    ids=["userinfo", "port", "space", "scheme", "empty"],
)
def test_an_unstorable_url_has_no_name(url: str) -> None:
    assert WebFetchName.maybe(url) is None
    with pytest.raises(ValueError, match="malformed URL"):
        WebFetchName.of(url)
