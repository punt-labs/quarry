"""Tests for quarry.web_fetch_name: the stored name of a WebFetch capture."""

from __future__ import annotations

import pytest

from quarry.web_fetch_name import WebFetchName
from tests.url_names import with_query


def test_the_name_is_the_storage_form_of_the_url() -> None:
    url = "https://x.test:8443/reset?email=user@example.com#frag"
    assert WebFetchName.of(url) == with_query(
        "https://x.test:8443/reset", "email=user@example.com"
    )


def test_a_fragment_does_not_make_a_second_document() -> None:
    base = WebFetchName.of("https://x.test/docs/guide")
    assert base == WebFetchName.of("https://x.test/docs/guide#section")


def test_pages_that_differ_only_by_query_are_two_documents() -> None:
    first = WebFetchName.of("https://x.test/docs/guide?page=1")
    second = WebFetchName.of("https://x.test/docs/guide?page=2")
    assert first != second
    assert first == with_query("https://x.test/docs/guide", "page=1")


def test_reordered_query_parameters_are_one_document() -> None:
    assert WebFetchName.of("https://x.test/g?a=1&b=2") == WebFetchName.of(
        "https://x.test/g?b=2&a=1"
    )


def test_a_trailing_slash_stays_a_distinct_document() -> None:
    assert WebFetchName.of("https://x.test/docs") != WebFetchName.of(
        "https://x.test/docs/"
    )


def test_an_email_in_the_path_is_scrubbed() -> None:
    name = WebFetchName.of("https://x.test/u/user@example.com/profile")
    assert name == "https://x.test/u/[REDACTED:email]/profile"


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
