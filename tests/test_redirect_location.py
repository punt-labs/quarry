"""Tests for quarry.ingestion.redirect_location: is the Location parseable."""

from __future__ import annotations

from http.client import HTTPMessage

import pytest

from quarry.ingestion.redirect_location import RedirectLocation

_BASE = "https://public.example/start"


def _headers(**fields: str) -> HTTPMessage:
    headers = HTTPMessage()
    for name, value in fields.items():
        headers[name] = value
    return headers


@pytest.mark.parametrize(
    "location", ["http://[", "http://[::1", "//[x/", "http://℀.com"]
)
def test_a_malformed_target_is_reported(location: str) -> None:
    assert RedirectLocation.is_malformed(_BASE, _headers(Location=location))


@pytest.mark.parametrize("location", ["/next", "https://other.example/x", "next?q=1"])
def test_a_parseable_target_is_not(location: str) -> None:
    assert not RedirectLocation.is_malformed(_BASE, _headers(Location=location))


def test_no_location_is_not_malformed() -> None:
    assert not RedirectLocation.is_malformed(_BASE, _headers())


def test_the_uri_header_is_read_when_there_is_no_location() -> None:
    assert RedirectLocation.is_malformed(_BASE, _headers(URI="http://["))


def test_an_empty_location_is_read_as_cpython_reads_it() -> None:
    # CPython takes the empty Location and never looks at URI.
    headers = _headers(Location="", URI="http://[")
    assert not RedirectLocation.is_malformed(_BASE, headers)
