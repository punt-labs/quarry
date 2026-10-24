"""Oracle property test for quarry.log_redactor against the spec in its docstring.

Log text is generated from a grammar and the redactor is held to three
properties: no secret survives, every non-URL line survives unchanged, and
redaction is idempotent.  The secret alphabet (``Q X Z J``) occurs nowhere else
in the grammar, so "no secret survives" is a plain character test, and a
fragment of a secret split by whitespace is caught as surely as a whole one.

The profile is derandomized: the same examples run in CI every time.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st

from quarry.log_redactor import LogRedactor
from tests.redaction import redacted

_MARKER_CHARS = "QXZJ"
# Every whitespace character urlsplit or a log reader may put inside a URL, except
# the newlines, which the grammar places explicitly.
_HSPACE = (" ", "\t", "\x0b", "\x0c", "\x1c", "\x85", "\xa0", "\u2003", "\u2028")
_SCHEMES = (
    "http",
    "https",
    "ftp",
    "ws",
    "wss",
    "s3",
    "git+ssh",
    "2http",
    "-x",
    "a" * 40,
)
_SEPARATORS = ("://", ":///", ":/ /", ": //", ":\t//", ":// ", ":\xa0//\xa0")
_HOSTS = ("h.example", "h.example:8080", "[::1]:80", "h")
_any_host = st.one_of(
    st.sampled_from(_HOSTS),
    st.text(alphabet="abcdefghij0123456789.-", min_size=1, max_size=12),
)
_TAILS = ("", "/", "/p/q", "/p?x=1#f", "?x=1", "#f")
_PREFIXES = ("", "GET ", "see ", "(", "url=", '"')
_SUFFIXES = ("", " ok", ")", ",", ".", '"')
_EMAIL = "alice@example.com wrote this"
_PROSE_LINES = (
    "Traceback (most recent call last):",
    '  File "/x/y.py", line 3, in f',
    "ERROR quarry.fetch: boom",
    "ValueError: nothing here",
    _EMAIL,
    "bob@example.org",
)

_word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8)
# Words and the punctuation that is not yet a URL when it stands alone.
_prose_token = st.one_of(
    _word, st.sampled_from([":", "@", "?", "://", "\\", "(", ")", "w:"])
)
_prose = st.one_of(
    st.sampled_from(_PROSE_LINES),
    st.lists(_word, min_size=1, max_size=6).map(" ".join),
    st.lists(_prose_token, min_size=1, max_size=8).map(" ".join),
)
_hspace = st.sampled_from(_HSPACE)
_secret = st.text(alphabet=_MARKER_CHARS, min_size=1, max_size=6)
# Secret characters with whitespace anywhere among them.
_spaced = st.lists(st.one_of(_secret, _hspace), min_size=0, max_size=6).map("".join)
_long = st.tuples(
    st.integers(2049, 5000), st.integers(0, 2049), _hspace, st.booleans()
).map(lambda t: "Q" * t[1] + (t[2] if t[3] else "") + "X" * (t[0] - t[1]))
_password = st.one_of(_spaced, _long)


# What the contract does not promise, so the generator never draws it:
#   (a) a user with no ``:`` broken by a gap, which cannot be told from a host-only
#       URL followed by an address;
#   (b) a credential broken by two line breaks (a continuation is a single hop);
#   (c) a password with ``@`` and then whitespace;
#   (d) ``?``, ``#`` or a backslash in a URL's password: each ends the authority,
#       so what precedes it is the host, and a quote after it ends the URL.
_punctuated = st.text(alphabet=_MARKER_CHARS + "@:'[]%", min_size=1, max_size=8).filter(
    lambda text: any(c in _MARKER_CHARS for c in text)
)


_plain_user = st.sampled_from(["bob", "u", "a.b", "admin", "x%40y"])


@st.composite
def _userinfo(draw: st.DrawFn) -> str:
    """Return ``user:password``: shaped by a ``_``, or by the ``:`` after a name."""
    if draw(st.booleans()):
        return "Q_" + draw(_spaced) + ":" + draw(_password)
    return draw(_plain_user) + ":" + draw(_punctuated)


@st.composite
def _secret_url(draw: st.DrawFn) -> str:
    sep = draw(st.sampled_from(_SEPARATORS))
    url = (
        f"{draw(st.sampled_from(_SCHEMES))}{sep}{draw(_userinfo())}"
        f"@{draw(_any_host)}{draw(st.sampled_from(_TAILS))}"
    )
    return draw(st.sampled_from(_PREFIXES)) + url + draw(st.sampled_from(_SUFFIXES))


@st.composite
def _split_url(draw: st.DrawFn) -> str:
    """Return two lines: a URL cut inside its userinfo, then the rest of it."""
    scheme = draw(st.sampled_from(_SCHEMES))
    head = f"{draw(st.sampled_from(_PREFIXES))}{scheme}://Q_{draw(_spaced)}:{draw(_secret)}"
    lead = draw(st.sampled_from(("", " ", "\t", "  ")))
    tail = f"{lead}{draw(_secret)}@h.example/p rest"
    return head + draw(st.sampled_from(("\n", "\r\n", "\r"))) + tail


@st.composite
def _two_urls(draw: st.DrawFn) -> str:
    """Return two credentialed URLs on one line."""
    return (
        draw(_secret_url())
        + draw(st.sampled_from((" ", ", ", " and ")))
        + draw(_secret_url())
    )


@st.composite
def _host_only(draw: st.DrawFn) -> str:
    """Return a URL without credentials, then a line holding an email address."""
    url = f"{draw(st.sampled_from(_SCHEMES))}://{draw(st.sampled_from(_HOSTS))}"
    glue = draw(st.sampled_from(("\n", " ", "\n\n", "\r\n")))
    return f"see {url}{glue}{_EMAIL}"


@st.composite
def _bare_userinfo(draw: st.DrawFn) -> str:
    """Return ``user:password@host`` with no scheme; the password may hold ``?#``."""
    password = draw(st.text(alphabet=_MARKER_CHARS + "?#\\", min_size=1, max_size=8))
    return f"{draw(_word)}:{password}@{draw(_any_host)}"


_lines = st.lists(
    st.one_of(
        _prose.map(lambda s: (False, s)),
        _secret_url().map(lambda s: (True, s)),
        _split_url().map(lambda s: (True, s)),
        _two_urls().map(lambda s: (True, s)),
        _bare_userinfo().map(lambda s: (True, s)),
        _host_only().map(lambda s: (False, s)),
    ),
    min_size=1,
    max_size=6,
)
_FUZZ = st.text(alphabet="ab:/@?# \n\r\x0b.[]%1", max_size=60)
_PROFILE = settings(derandomize=True, max_examples=300, deadline=None, database=None)


def _text(lines: list[tuple[bool, str]]) -> str:
    return "\n".join(line for _, line in lines)


@_PROFILE
@given(_lines)
def test_no_secret_survives(lines: list[tuple[bool, str]]) -> None:
    shown = redacted(_text(lines))
    assert not set(shown) & set(_MARKER_CHARS), shown


@_PROFILE
@given(st.lists(_prose, min_size=1, max_size=8))
def test_every_non_url_line_survives_unchanged(lines: list[str]) -> None:
    text = "\n".join(lines)
    assert redacted(text) == text


@_PROFILE
@given(_host_only())
def test_a_host_only_url_leaves_the_email_after_it_alone(text: str) -> None:
    assert _EMAIL in redacted(text)


@_PROFILE
@given(st.lists(_prose, min_size=0, max_size=3), _split_url(), _prose)
def test_lines_around_a_split_url_survive(
    before: list[str], split: str, after: str
) -> None:
    shown = redacted("\n".join([*before, split, after]))
    assert shown.endswith("\n" + after)
    assert shown.startswith("\n".join(before)) or not before


_DELIMITERS = ")].,:;>'\""


@st.composite
def _url_then_delimiters(draw: st.DrawFn) -> tuple[str, str]:
    """Return a URL with no userinfo, query or fragment, and trailing delimiters."""
    path = draw(st.sampled_from(("", "/", "/p/q")))
    url = f"{draw(st.sampled_from(_SCHEMES))}://{draw(_any_host)}{path}"
    # A ``:`` after a bare host is a missing port, and the host is masked.
    alphabet = _DELIMITERS if path else _DELIMITERS.replace(":", "")
    return url, draw(st.text(alphabet=alphabet, min_size=1, max_size=4))


@_PROFILE
@given(_url_then_delimiters())
def test_the_delimiters_after_a_url_survive_exactly(case: tuple[str, str]) -> None:
    url, delimiters = case
    shown = redacted(f"see {url}{delimiters}")
    assert shown.endswith(delimiters), shown


@_PROFILE
@given(
    st.sampled_from(_SCHEMES),
    _any_host,
    _secret,
    st.text(alphabet=".,:;", min_size=1, max_size=4),
    st.sampled_from(("?t=", "#")),
)
def test_a_secret_query_or_fragment_never_shows_the_delimiters_after_it(
    scheme: str, host: str, secret: str, delimiters: str, opener: str
) -> None:
    shown = redacted(f"see {scheme}://{host}/p{opener}{secret}{delimiters}")
    assert not set(shown) & set(_MARKER_CHARS), shown
    assert not shown.endswith(tuple(delimiters)), shown


_value_with_quotes = (
    st.lists(
        st.one_of(_secret, st.sampled_from(("'", '"', "<", ">"))),
        min_size=1,
        max_size=6,
    )
    .map("".join)
    .filter(lambda text: any(c in _MARKER_CHARS for c in text))
)


@_PROFILE
@given(
    st.sampled_from(_SCHEMES),
    _any_host,
    _value_with_quotes,
    st.sampled_from(("?t=", "#")),
)
def test_a_query_or_fragment_secret_holding_quotes_never_survives(
    scheme: str, host: str, value: str, opener: str
) -> None:
    shown = redacted(f"see {scheme}://{host}/p{opener}{value} end")
    assert not set(shown) & set(_MARKER_CHARS), shown
    assert redacted(shown) == shown


@_PROFILE
@given(
    st.sampled_from(("GET /p", "host/p", "see h.example/a/b")),
    _value_with_quotes,
)
def test_a_scheme_less_query_secret_never_survives(prefix: str, value: str) -> None:
    shown = redacted(f"{prefix}?{value} end")
    assert not set(shown) & set(_MARKER_CHARS), shown
    assert redacted(shown) == shown


@_PROFILE
@given(
    st.sampled_from(_SCHEMES),
    _any_host,
    _secret,
    st.text(alphabet=")]", min_size=1, max_size=4),
    st.sampled_from(("?t=", "#")),
)
def test_a_secret_ending_in_closers_with_no_opener_never_shows_them(
    scheme: str, host: str, secret: str, closers: str, opener: str
) -> None:
    shown = redacted(f"see {scheme}://{host}/p{opener}{secret}{closers}")
    assert not set(shown) & set(_MARKER_CHARS), shown
    # The log form carries its own balanced ``(segments=N)``; nothing else closes.
    assert shown.count(")") <= shown.count("("), shown
    assert shown.count("]") <= shown.count("["), shown


@_PROFILE
@given(st.sampled_from(_SCHEMES), _secret)
def test_a_bare_label_before_a_colon_is_never_shown(scheme: str, label: str) -> None:
    shown = redacted(f"see {scheme}://{label}: and more")
    assert not set(shown) & set(_MARKER_CHARS), shown


@_PROFILE
@given(_url_then_delimiters())
def test_a_url_with_trailing_delimiters_is_idempotent(case: tuple[str, str]) -> None:
    once = redacted("see " + "".join(case))
    assert redacted(once) == once


def _passes_needed(text: str) -> int:
    count = 0
    while (after := LogRedactor._once(text)) != text:
        text, count = after, count + 1
    return count


@_PROFILE
@given(_lines)
def test_no_generated_case_comes_near_the_pass_cap(
    lines: list[tuple[bool, str]],
) -> None:
    assert _passes_needed(_text(lines)) < LogRedactor.PASSES - 1


@_PROFILE
@given(_lines)
def test_redaction_is_idempotent(lines: list[tuple[bool, str]]) -> None:
    once = redacted(_text(lines))
    assert redacted(once) == once


@_PROFILE
@given(_FUZZ)
def test_arbitrary_url_shaped_text_is_idempotent(text: str) -> None:
    once = redacted(text)
    assert redacted(once) == once


# Inputs that once leaked, kept as explicit examples.  The corpus and
# whitespace tests in test_log_redactor.py hold the rest of the proof inputs.
_PROOFS = (
    "see https://u:SECRET@h.example/p?x=1 end",
    "https://u:" + "Q" * 2049 + " X@h.example/p",
    "https://u:" + "Q" * 5000 + "\x0bX@h.example/p",
    "https://u:" + "Q" * 2047 + "\xa0X@h.example/p",
    "https:///u:Q X@h.example/p",
    "https:/ /u:Q\xa0X@h.example/p",
    "h t t p s : / / u:Q X@h.example/p",
    "https://u:Q\nX@h.example/p",
    "https://u:Q\n@h.example/p",
    "https://u:Q\r\nX@h.example/p",
    "https://\nu:Q@h.example/p",
    "https://admin:Q X@h.example/x",
    "-http://u:Q Q@h/x",
    "2http://u:Q Q@h/x",
    "1.http://u:Q Q@h/x",
    "+https://u:Q Q@h/x",
    "a" * 40 + "://u:Q Q@h/x",
    "x https://host.com foo:Q@baz.com",
    "GET https://api.io a:Q@c.io/x",
    "admin:Q?X@db.internal",
    "admin:Q#X@db",
)


@pytest.mark.parametrize("text", _PROOFS)
def test_the_proof_inputs_leave_no_secret(text: str) -> None:
    shown = redacted(text)
    assert not set(shown) & set(_MARKER_CHARS)
    assert "SECRET" not in shown
