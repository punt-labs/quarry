"""Tests for quarry.http_url: an admitted URL that prints without its secrets."""

from __future__ import annotations

import pytest

from quarry.http_url import HttpUrl

_RAW = "HTTPS://Example.COM:8443/docs/guide;sess=PSEC?token=QSEC#FSEC"


class TestFetchUrl:
    def test_the_fetchable_form_is_the_admitted_input(self) -> None:
        assert HttpUrl(_RAW).fetch_url == _RAW

    def test_equal_urls_are_equal_and_hash_alike(self) -> None:
        assert HttpUrl(_RAW) == HttpUrl(_RAW)
        assert len({HttpUrl(_RAW), HttpUrl(_RAW)}) == 1
        assert HttpUrl(_RAW) != HttpUrl("https://example.com/")

    def test_it_is_immutable(self) -> None:
        url = HttpUrl(_RAW)
        with pytest.raises(AttributeError):
            object.__setattr__(url, "extra", 1)


class TestRedactedForms:
    def test_str_is_the_log_form_with_no_path_at_all(self) -> None:
        assert str(HttpUrl(_RAW)) == "https://example.com:8443/…(2 segments)"

    def test_repr_names_the_type_and_shows_only_the_log_form(self) -> None:
        assert (
            repr(HttpUrl(_RAW)) == "HttpUrl('https://example.com:8443/…(2 segments)')"
        )

    @pytest.mark.parametrize(
        ("segment", "needle"),
        [
            ("9f8e7d6c5b4a39281706f5e4d3c2b1a0", "9f8e7d6c"),
            ("123e4567-e89b-12d3-a456-426614174000", "123e4567"),
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl", "eyJhbGci"),
            ("dGhpcy1pcy1hLXRva2Vu==", "dGhpcy1p"),
            ("abcdefghijklmnopqrstuvwxyz", "abcdefgh"),
            ("x7Qp", "x7Qp"),
            ("2024-05-17-launch-notes", "launch-notes"),
        ],
        ids=["hex", "uuid", "jwt", "padded-base64", "all-alpha", "short-code", "slug"],
    )
    def test_no_path_segment_reaches_the_log_form(
        self, segment: str, needle: str
    ) -> None:
        shown = str(HttpUrl(f"https://example.com/a/{segment}/b"))
        assert needle not in shown
        assert shown == "https://example.com/…(3 segments)"

    def test_a_bare_host_has_no_segments(self) -> None:
        assert str(HttpUrl("http://example.com")) == "http://example.com/…(0 segments)"
        assert str(HttpUrl("http://example.com/")) == "http://example.com/…(0 segments)"

    def test_one_segment_is_singular(self) -> None:
        assert str(HttpUrl("http://example.com/a")) == "http://example.com/…(1 segment)"

    @pytest.mark.parametrize("raw", ["http://example.com", "http://example.com/"])
    def test_a_bare_host_has_one_stored_name(self, raw: str) -> None:
        assert HttpUrl(raw).document_name == "http://example.com/"

    def test_the_stored_name_keeps_the_path_but_not_secrets(self) -> None:
        url = HttpUrl("https://example.com/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0?t=Q")
        assert url.document_name == (
            "https://example.com/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0"
        )


class TestDocumentName:
    @pytest.mark.parametrize(
        ("raw", "name"),
        [
            (_RAW, "https://example.com:8443/docs/guide"),
            ("http://example.com", "http://example.com/"),
            ("http://[2606:4700::1111]:8080/a?x=1", "http://[2606:4700::1111]:8080/a"),
            ("http://Example.com./a#f", "http://example.com/a"),
            ("http://example.com:80/a", "http://example.com/a"),
            ("https://example.com:443/a", "https://example.com/a"),
            ("http://example.com:443/a", "http://example.com:443/a"),
            ("https://example.com:80/a", "https://example.com:80/a"),
            ("http://example\u3002com\uff0e/a", "http://example.com/a"),
            ("http://example.com\uff61/a", "http://example.com/a"),
            ("https://example.com/docs/", "https://example.com/docs/"),
        ],
        ids=[
            "full",
            "bare-host",
            "ipv6-port",
            "trailing-dot",
            "http-default-port",
            "https-default-port",
            "http-on-443",
            "https-on-80",
            "ideographic-full-stop",
            "halfwidth-ideographic-stop",
            "trailing-slash",
        ],
    )
    def test_scheme_host_port_and_path_only(self, raw: str, name: str) -> None:
        assert HttpUrl(raw).document_name == name

    def test_urls_differing_only_in_query_or_fragment_share_a_name(self) -> None:
        names = {
            HttpUrl(u).document_name
            for u in (
                "https://x.test/a",
                "https://x.test/a?utm=1",
                "https://x.test/a#top",
            )
        }
        assert len(names) == 1


class TestParts:
    def test_the_host_is_normalized_like_idna_does(self) -> None:
        assert HttpUrl("http://Example\u3002Com.:81/a").host == "example.com"

    def test_host_and_path_are_the_admitted_parts(self) -> None:
        url = HttpUrl(_RAW)
        assert (url.host, url.path) == ("example.com", "/docs/guide")

    def test_an_ipv6_host_has_no_brackets(self) -> None:
        assert HttpUrl("http://[2606:4700::1111]:80/a").host == "2606:4700::1111"

    def test_a_bare_host_has_an_empty_path(self) -> None:
        assert HttpUrl("http://example.com").path == ""


class TestHomepage:
    def test_it_is_built_from_host_and_port_never_the_raw_netloc(self) -> None:
        assert HttpUrl(_RAW).homepage == HttpUrl("https://example.com:8443/")

    def test_an_ipv6_homepage_is_bracketed(self) -> None:
        home = HttpUrl("http://[2606:4700::1111]/a").homepage
        assert home.fetch_url == "http://[2606:4700::1111]/"


class TestConstructor:
    @pytest.mark.parametrize(
        "raw",
        [
            "http://u:SECRET@example.com/",
            "http://example.com:99999/",
            "http://example.com/a b",
            "ftp://example.com/",
            "http:///path",
            "example.com/path",
            "",
        ],
        ids=["userinfo", "port", "space", "scheme", "no-host", "no-scheme", "empty"],
    )
    def test_a_refusal_has_fixed_text_and_no_cause(self, raw: str) -> None:
        with pytest.raises(ValueError, match="malformed URL") as info:
            HttpUrl(raw)
        assert str(info.value) == "malformed URL"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None or info.value.__suppress_context__


class TestShown:
    """``HttpUrl.shown`` is the one producer of a URL's log text."""

    def test_an_admitted_url_shows_its_log_form(self) -> None:
        assert HttpUrl.shown(_RAW) == str(HttpUrl(_RAW))

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://u:secret@h.test/a/b?t=1", "http://h.test/…(2 segments)"),
            ("http://h.test/" + "a/" * 5000, "http://h.test/…(5000 segments)"),
            ("http://h.test:8080/a\nb/c", "http://h.test:8080/…(2 segments)"),
        ],
    )
    def test_a_refused_url_shows_the_same_shape_and_never_a_path(
        self, raw: str, expected: str
    ) -> None:
        with pytest.raises(ValueError, match="malformed URL"):
            HttpUrl(raw)
        assert HttpUrl.shown(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["http://u:s@h:99999/x?t=1", "ftp://h/a", "not a url", "http:///a", ""],
    )
    def test_an_unreadable_url_shows_a_fixed_placeholder(self, raw: str) -> None:
        assert HttpUrl.shown(raw) == "(unparseable URL)"

    def test_a_host_too_long_to_be_a_host_is_not_shown(self) -> None:
        assert HttpUrl.shown("http://" + "h" * 300 + "/a b") == "(unparseable URL)"
