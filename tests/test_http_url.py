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
    def test_str_is_the_log_form_without_query_fragment_or_params(self) -> None:
        assert str(HttpUrl(_RAW)) == "https://example.com:8443/docs/guide"

    def test_repr_names_the_type_and_shows_only_the_log_form(self) -> None:
        assert repr(HttpUrl(_RAW)) == "HttpUrl('https://example.com:8443/docs/guide')"

    @pytest.mark.parametrize(
        "path",
        [
            "/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0",
            "/u/123e4567-e89b-12d3-a456-426614174000/profile",
            "/t/AbCdEf0123456789xyzXYZ/page",
        ],
        ids=["hex-token", "uuid", "mixed-token"],
    )
    def test_a_high_entropy_path_segment_is_elided_from_the_log_form(
        self, path: str
    ) -> None:
        shown = str(HttpUrl(f"https://example.com{path}"))
        assert "9f8e7d6c" not in shown
        assert "123e4567" not in shown
        assert "AbCdEf" not in shown
        assert "/[…]" in shown

    def test_a_readable_path_is_kept(self) -> None:
        url = HttpUrl("https://example.com/documentation-overview/getting-started")
        assert str(url) == "https://example.com/documentation-overview/getting-started"

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
            ("http://example.com", "http://example.com"),
            ("http://[2606:4700::1111]:8080/a?x=1", "http://[2606:4700::1111]:8080/a"),
            ("http://Example.com./a#f", "http://example.com./a"),
            ("https://example.com/docs/", "https://example.com/docs/"),
        ],
        ids=["full", "bare-host", "ipv6-port", "trailing-dot", "trailing-slash"],
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
