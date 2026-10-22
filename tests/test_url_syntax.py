"""Tests for quarry.url_syntax: the one syntactic admission rule for a URL."""

from __future__ import annotations

import pytest

from quarry.url_syntax import UrlSyntax

_PUB = "93.184.216.34"
_MARKER = "MARKER"

# Every URL here must be refused, and the refusal must name nothing of the input.
_MALFORMED = {
    "bad_port": f"http://u:{_MARKER}@{_PUB}:99999/a?token={_MARKER}",
    "port_zero": f"http://{_PUB}:0/a?token={_MARKER}",
    "port_negative": f"http://{_PUB}:-1/a?token={_MARKER}",
    "port_non_numeric": f"http://{_PUB}:abc/a?token={_MARKER}",
    "port_unicode_digits": f"http://{_PUB}:\u0665\u0665/a?token={_MARKER}",
    "port_plus": f"http://{_PUB}:+80/a?token={_MARKER}",
    "port_space": f"http://{_PUB}: 80/a?token={_MARKER}",
    "port_underscore": f"http://{_PUB}:8_0/a?token={_MARKER}",
    "space_in_query": f"http://{_PUB}/a?token={_MARKER} x",
    "space_in_path": f"http://{_PUB}/a b?token={_MARKER}",
    "control_in_query": f"http://{_PUB}/a?token={_MARKER}\x01x",
    "tab_in_host": f"http://{_PUB}\t/a?token={_MARKER}",
    "newline_in_path": f"http://{_PUB}/a\nb?token={_MARKER}",
    "leading_space": f" http://{_PUB}/a?token={_MARKER}",
    "leading_control": f"\x01http://{_PUB}/a?token={_MARKER}",
    "c1_control": f"http://{_PUB}/a\x85?token={_MARKER}",
    "unicode_space": f"http://{_PUB}/a\u00a0b?token={_MARKER}",
    "ipv6_unclosed": f"http://[::1/a?token={_MARKER}",
    "ipv6_zone": f"http://[fe80::1%25eth0]/a?token={_MARKER}",
    "userinfo_password": f"http://user:{_MARKER}@{_PUB}/a?token={_MARKER}",
    "userinfo_bare": f"http://{_MARKER}@{_PUB}/a",
    "userinfo_second_at": f"http://u:{_MARKER}@x@{_PUB}/a",
    "userinfo_empty_host": f"http://u:{_MARKER}@/a?token={_MARKER}",
    "nfkc_hash": f"http://u:{_MARKER}@\uff03{_PUB}/a",
    "nfkc_at": f"http://u:{_MARKER}\uff20{_PUB}/a",
    "nfkc_slash": f"http://{_PUB}\uff0f@evil/a?token={_MARKER}",
    "backslash": f"http://{_PUB}\\@evil.com/a?token={_MARKER}",
    "percent_host": f"http://%39%33.184.216.34/a?token={_MARKER}",
}

# These stay valid: the rules must not reject a legitimate URL.
_LEGITIMATE = {
    "mixed_case_scheme": f"HtTp://{_PUB}/a?token={_MARKER}",
    "https_default": "https://example.com/",
    "explicit_port": "http://example.com:8080/a",
    "top_port": "http://example.com:65535/a",
    "ipv6_literal": "http://[2606:4700::1111]/a",
    "ipv6_literal_port": "http://[2606:4700::1111]:8443/a",
    "trailing_dot": f"http://{_PUB}./a",
    "punycode": "https://xn--bcher-kva.example/",
    "fragment_with_at": f"http://{_PUB}#@evil.com/",
    "query_with_at": f"http://{_PUB}/a?next=x@y",
}


@pytest.mark.parametrize("raw", _MALFORMED.values(), ids=_MALFORMED.keys())
def test_a_malformed_url_is_refused_with_fixed_text(raw: str) -> None:
    with pytest.raises(ValueError, match="malformed URL") as info:
        UrlSyntax.parse(raw)
    assert str(info.value) == "malformed URL"
    assert info.value.__cause__ is None
    assert info.value.__context__ is None or info.value.__suppress_context__


@pytest.mark.parametrize("raw", _LEGITIMATE.values(), ids=_LEGITIMATE.keys())
def test_a_legitimate_url_is_admitted(raw: str) -> None:
    assert UrlSyntax.parse(raw).geturl()


def test_the_parse_result_is_what_every_later_parser_sees() -> None:
    parts = UrlSyntax.parse("HTTPS://Example.com:8443/a/b?x=1#f")
    assert (parts.scheme, parts.hostname, parts.port) == ("https", "example.com", 8443)
    assert (parts.path, parts.query, parts.fragment) == ("/a/b", "x=1", "f")


def test_a_url_over_the_length_cap_is_refused() -> None:
    long = "http://example.com/" + "a" * 8200
    with pytest.raises(ValueError, match="malformed URL"):
        UrlSyntax.parse(long)


def test_a_url_at_the_length_cap_is_admitted() -> None:
    url = "http://example.com/" + "a" * (8192 - len("http://example.com/"))
    assert UrlSyntax.parse(url).hostname == "example.com"
