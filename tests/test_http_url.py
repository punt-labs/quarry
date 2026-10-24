"""Tests for quarry.http_url: an admitted URL that prints without its secrets."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from quarry.http_url import HttpUrl
from tests.url_names import with_query

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
        assert str(HttpUrl(_RAW)) == "https://example.com:8443/…(segments=2)"

    def test_repr_names_the_type_and_shows_only_the_log_form(self) -> None:
        assert (
            repr(HttpUrl(_RAW)) == "HttpUrl('https://example.com:8443/…(segments=2)')"
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
        assert shown == "https://example.com/…(segments=3)"

    def test_a_bare_host_has_no_segments(self) -> None:
        assert str(HttpUrl("http://example.com")) == "http://example.com/…(segments=0)"
        assert str(HttpUrl("http://example.com/")) == "http://example.com/…(segments=0)"

    def test_one_segment_is_singular(self) -> None:
        assert (
            str(HttpUrl("http://example.com/a")) == "http://example.com/…(segments=1)"
        )

    @pytest.mark.parametrize("raw", ["http://example.com", "http://example.com/"])
    def test_a_bare_host_has_one_stored_name(self, raw: str) -> None:
        assert HttpUrl(raw).document_name == "http://example.com/"

    def test_the_stored_name_keeps_the_path_but_not_secrets(self) -> None:
        url = HttpUrl("https://example.com/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0?t=Q")
        assert url.document_name == with_query(
            "https://example.com/reset/9f8e7d6c5b4a39281706f5e4d3c2b1a0", "t=Q"
        )
        assert "t=Q" not in url.document_name


class TestDocumentName:
    @pytest.mark.parametrize(
        ("raw", "name"),
        [
            (_RAW, with_query("https://example.com:8443/docs/guide", "token=QSEC")),
            ("http://example.com", "http://example.com/"),
            (
                "http://[2606:4700::1111]:8080/a?x=1",
                with_query("http://[2606:4700::1111]:8080/a", "x=1"),
            ),
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

    def test_urls_differing_only_in_fragment_or_params_share_a_name(self) -> None:
        names = {
            HttpUrl(u).document_name
            for u in (
                "https://x.test/a",
                "https://x.test/a#top",
                "https://x.test/a;sess=1",
                "https://x.test/a?#top",
            )
        }
        assert names == {"https://x.test/a"}


class TestQueryDigest:
    """A query is part of a document's identity but never part of its name."""

    def test_pages_differing_only_by_query_are_two_documents(self) -> None:
        first = HttpUrl("https://x.test/list?page=1").document_name
        second = HttpUrl("https://x.test/list?page=2").document_name
        assert first != second

    def test_reordered_parameters_are_one_document(self) -> None:
        a = HttpUrl("https://x.test/list?b=2&a=1").document_name
        b = HttpUrl("https://x.test/list?a=1&b=2").document_name
        assert a == b

    def test_empty_parameters_do_not_change_the_document(self) -> None:
        a = HttpUrl("https://x.test/list?a=1&&b=2&").document_name
        assert a == HttpUrl("https://x.test/list?a=1&b=2").document_name

    def test_the_stored_name_never_holds_the_raw_query(self) -> None:
        name = HttpUrl(
            "https://x.test/p?token=SECRET&user=jane@example.com"
        ).document_name
        assert "SECRET" not in name
        assert "jane" not in name
        assert name == with_query(
            "https://x.test/p", "token=SECRET&user=jane@example.com"
        )

    def test_the_digest_is_sixteen_hex_digits_of_the_sorted_query(self) -> None:
        name = HttpUrl("https://x.test/p?z=1&a=2").document_name
        assert re.fullmatch(r"https://x\.test/p\?q\[sha256\]=[0-9a-f]{16}", name)

    def test_the_encoding_of_a_parameter_does_not_make_a_second_document(
        self,
    ) -> None:
        raw = HttpUrl("https://x.test/p?q=caf\u00e9").document_name
        assert raw == HttpUrl("https://x.test/p?q=caf%c3%a9").document_name

    def test_a_stored_name_reads_back_as_itself(self) -> None:
        name = HttpUrl("https://x.test/p?page=3").document_name
        assert HttpUrl.stored(name) == name

    def test_stored_canonicalizes_what_is_not_yet_a_stored_name(self) -> None:
        assert HttpUrl.stored("https://X.test:443/p?b=2&a=1#f") == (
            HttpUrl("https://x.test/p?a=1&b=2").document_name
        )

    def test_a_stored_name_with_an_old_host_spelling_is_canonicalized(self) -> None:
        name = HttpUrl("https://x.test/p?page=3").document_name
        assert HttpUrl.stored(name.replace("x.test", "X.TEST:443")) == name

    def test_a_real_query_that_looks_like_a_digest_is_digested_like_any_other(
        self,
    ) -> None:
        for forged, encoded in (
            ("q=0123456789abcdef", "q=0123456789abcdef"),
            ("q[sha256]=0123456789abcdef", "q%5Bsha256%5D=0123456789abcdef"),
        ):
            name = HttpUrl(f"https://x.test/p?{forged}").document_name
            assert name == with_query("https://x.test/p", encoded)

    def test_the_marker_cannot_come_out_of_a_real_query(self) -> None:
        # [ and ] are percent-encoded in a real query, so no input yields the marker.
        name = HttpUrl("https://x.test/p?a[sha256]=b").document_name
        assert name == with_query("https://x.test/p", "a%5Bsha256%5D=b")

    def test_the_fetch_url_keeps_the_whole_query(self) -> None:
        assert HttpUrl("https://x.test/p?b=2&a=1#f").fetch_url == (
            "https://x.test/p?b=2&a=1#f"
        )

    def test_the_log_form_shows_no_query(self) -> None:
        assert str(HttpUrl("https://x.test/p?token=SECRET")) == (
            "https://x.test/…(segments=1)"
        )


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


class TestPathParametersAndPorts:
    def test_path_parameters_are_dropped_and_the_segments_counted_without_them(
        self,
    ) -> None:
        url = HttpUrl("https://example.com/a;x/b")
        assert url.path == "/a/b"
        assert url.document_name == "https://example.com/a/b"
        assert str(url) == "https://example.com/…(segments=2)"

    def test_the_log_form_drops_the_default_port(self) -> None:
        assert str(HttpUrl("https://example.com:443/a")) == (
            "https://example.com/…(segments=1)"
        )
        assert str(HttpUrl("https://example.com:8443/a")) == (
            "https://example.com:8443/…(segments=1)"
        )


class TestShown:
    """``HttpUrl.shown`` is the one producer of a URL's log text."""

    def test_an_admitted_url_shows_its_log_form(self) -> None:
        assert HttpUrl.shown(_RAW) == str(HttpUrl(_RAW))

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://u:secret@h.test/a/b?t=1", "http://h.test/…(segments=2)"),
            ("http://h.test/" + "a/" * 5000, "http://h.test/…(segments=5000)"),
            ("http://h.test:8080/a\nb/c", "http://h.test:8080/…(segments=2)"),
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


class TestHomepageIsComputedOnce:
    def test_the_homepage_is_computed_once(self) -> None:
        url = HttpUrl("https://example.com:8443/a/b?q=1")
        assert url.homepage is url.homepage

    def test_the_homepage_is_the_site_root(self) -> None:
        assert HttpUrl("https://example.com:8443/a/b?q=1").homepage == HttpUrl(
            "https://example.com:8443/"
        )

    def test_the_homepage_of_a_root_is_itself(self) -> None:
        url = HttpUrl("https://example.com/")
        assert url.homepage is url


class TestImmutability:
    """An ``HttpUrl`` is hashable, so it must also be unchangeable."""

    @pytest.mark.parametrize("name", ["_fetch_url", "_host", "extra"])
    def test_an_attribute_cannot_be_assigned(self, name: str) -> None:
        with pytest.raises(AttributeError, match="immutable"):
            setattr(HttpUrl(_RAW), name, "http://evil.test/")

    @pytest.mark.parametrize("name", ["_fetch_url", "_host"])
    def test_an_attribute_cannot_be_deleted(self, name: str) -> None:
        with pytest.raises(AttributeError, match="immutable"):
            delattr(HttpUrl(_RAW), name)


class TestShownLogsTheRefusal:
    def test_a_refused_url_leaves_a_debug_line_with_the_class_only(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = "http://u:SECRETPW@h.test/SECRETPATH?t=QSEC"
        with caplog.at_level(logging.DEBUG, logger="quarry.http_url"):
            HttpUrl.shown(raw)
        [record] = caplog.records
        assert record.levelno == logging.DEBUG
        assert "ValueError" in record.getMessage()
        for secret in ("SECRETPW", "SECRETPATH", "QSEC"):
            assert secret not in caplog.text

    def test_an_admitted_url_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="quarry.http_url"):
            HttpUrl.shown("https://h.test/a")
        assert caplog.records == []


class TestInternationalizedPaths:
    """An IRI is legitimate input: it is percent-encoded once, at construction."""

    def test_the_fetchable_form_is_ascii(self) -> None:
        url = HttpUrl("https://example.com/café/日本?q=ü#fräg")
        assert url.fetch_url.isascii()
        assert url.fetch_url == (
            "https://example.com/caf%C3%A9/%E6%97%A5%E6%9C%AC?q=%C3%BC#fr%C3%A4g"
        )

    def test_the_two_spellings_are_one_document(self) -> None:
        iri = HttpUrl("https://example.com/café")
        encoded = HttpUrl("https://example.com/caf%C3%A9")
        assert iri.document_name == encoded.document_name
        assert iri.document_name == "https://example.com/caf%C3%A9"
        assert iri.fetch_url == encoded.fetch_url

    def test_lowercase_escapes_are_normalized_to_uppercase(self) -> None:
        url = HttpUrl("https://example.com/caf%c3%a9?q=%c3%bc")
        assert url.fetch_url == "https://example.com/caf%C3%A9?q=%C3%BC"
        assert url.document_name == with_query(
            "https://example.com/caf%C3%A9", "q=%C3%BC"
        )

    def test_a_lone_percent_is_escaped_not_dropped(self) -> None:
        assert HttpUrl("https://example.com/100%").document_name == (
            "https://example.com/100%25"
        )

    def test_an_ascii_url_is_unchanged(self) -> None:
        raw = "https://example.com/a/b;p=1?x=1&y=2#f"
        assert HttpUrl(raw).fetch_url == raw

    def test_the_log_form_is_unaffected(self) -> None:
        assert str(HttpUrl("https://example.com/café/x")) == (
            "https://example.com/…(segments=2)"
        )

    def test_no_encode_error_can_reach_the_request_line(self) -> None:
        from http.client import HTTPConnection

        fetch = HttpUrl("https://example.com/ü?x=é#è").fetch_url
        HTTPConnection("example.com").putrequest("GET", fetch)

    def test_a_percent_in_the_host_is_still_refused(self) -> None:
        with pytest.raises(ValueError, match=r"^malformed URL$"):
            HttpUrl("https://exa%6Dple.com/p")


class TestStoredHasOnlyTheCallersItIsMeantFor:
    """``HttpUrl.stored`` recognises the digest marker, so a fetch must never use it."""

    def test_only_the_name_readers_call_it(self) -> None:
        src = Path(__file__).resolve().parent.parent / "src" / "quarry"
        callers = {
            path.relative_to(src).as_posix()
            for path in src.rglob("*.py")
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Attribute)
            and node.attr == "stored"
            and isinstance(node.value, ast.Name)
            and node.value.id == "HttpUrl"
        }
        assert callers == {"ingestion/legacy_url_names.py"}
