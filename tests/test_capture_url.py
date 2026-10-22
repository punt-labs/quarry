"""Tests for quarry.capture_url — PII-safe capture URL metadata."""

from __future__ import annotations

from quarry.capture_url import CaptureUrl
from quarry.scrub import scrub_and_log


def _scrub(text: str) -> str:
    """The same text scrubber the WebFetch capture path passes in."""
    return scrub_and_log(text, "test")


def test_redacted_strips_query_and_fragment() -> None:
    """A reset link's email/token in the query never survive into metadata."""
    url = "https://x.test/reset?email=user@example.com&token=abc#frag"
    meta = CaptureUrl(url).redacted(_scrub)
    assert meta == "https://x.test/reset"
    assert "user@example.com" not in meta
    assert "token=abc" not in meta
    assert "abc" not in meta
    assert "frag" not in meta


def test_redacted_strips_userinfo() -> None:
    """``user:pass@host`` credentials are dropped from the metadata URL."""
    meta = CaptureUrl("https://alice:s3cret@x.test/page").redacted(_scrub)
    assert meta == "https://x.test/page"
    assert "alice" not in meta
    assert "s3cret" not in meta


def test_redacted_preserves_scheme_host_port_path() -> None:
    """The useful location — scheme, host, port, path — is retained."""
    meta = CaptureUrl("https://x.test:8443/docs/guide").redacted(_scrub)
    assert meta == "https://x.test:8443/docs/guide"


def test_redacted_scrubs_email_in_path() -> None:
    """Defence in depth: an email in the path itself is still redacted."""
    meta = CaptureUrl("https://x.test/u/user@example.com/profile").redacted(_scrub)
    assert "user@example.com" not in meta
    assert "[REDACTED:email]" in meta


def test_redacted_plain_url_unchanged() -> None:
    """A URL with no userinfo/query/fragment round-trips unchanged."""
    meta = CaptureUrl("https://example.com/page").redacted(_scrub)
    assert meta == "https://example.com/page"


def test_redacted_ipv6_host_keeps_brackets() -> None:
    """An IPv6 literal host stays bracketed so its colons aren't read as a port."""
    meta = CaptureUrl("https://[2001:db8::1]/path").redacted(_scrub)
    assert meta == "https://[2001:db8::1]/path"


def test_redacted_ipv6_host_with_port_keeps_brackets() -> None:
    """Brackets survive alongside a port, and the query is still dropped."""
    meta = CaptureUrl("https://[2001:db8::1]:8080/p?x=1").redacted(_scrub)
    assert meta == "https://[2001:db8::1]:8080/p"


def test_redacted_ipv6_host_strips_userinfo() -> None:
    """Userinfo is dropped while the bracketed IPv6 host is preserved."""
    meta = CaptureUrl("https://u:pw@[2001:db8::1]/p").redacted(_scrub)
    assert meta == "https://[2001:db8::1]/p"
    assert "pw" not in meta


def test_redacted_trailing_slash_not_normalized() -> None:
    """A trailing-slash difference is NOT normalized — the two are distinct.

    Unlike query strings and fragments (stripped outright), the path is kept
    verbatim, so ``/docs`` and ``/docs/`` redact to different metadata URLs
    and will not match for lookup purposes.
    """
    without = CaptureUrl("https://x.test/docs").redacted(_scrub)
    with_slash = CaptureUrl("https://x.test/docs/").redacted(_scrub)
    assert without == "https://x.test/docs"
    assert with_slash == "https://x.test/docs/"
    assert without != with_slash


def test_display_is_total_and_never_echoes_an_unparseable_url() -> None:
    assert CaptureUrl.display("http://u:secret@h:99999/a?token=t") == (
        "(unparseable URL)"
    )


def test_display_of_a_good_url_drops_userinfo_query_and_fragment() -> None:
    assert CaptureUrl.display("https://u:pw@x.test/p?t=1#f") == "https://x.test/p"
