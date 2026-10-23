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

_MARKER_CHARS = "QXZJ"
# Every whitespace character urlsplit or a log reader may put inside a URL, except
# the newlines, which the grammar places explicitly.
_HSPACE = (" ", "\t", "\x0b", "\x0c", "\x1c", "\x85", "\xa0", "\u2003", "\u2028")
_SCHEMES = ("http", "https", "ftp", "ws", "wss", "s3", "git+ssh")
_SEPARATORS = ("://", ":///", ":/ /", ": //", ":\t//", ":// ", ":\xa0//\xa0")
_HOSTS = ("h.example", "h.example:8080", "[::1]:80", "h")
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
_prose = st.one_of(
    st.sampled_from(_PROSE_LINES),
    st.lists(_word, min_size=1, max_size=6).map(" ".join),
)
_hspace = st.sampled_from(_HSPACE)
_secret = st.text(alphabet=_MARKER_CHARS, min_size=1, max_size=6)
# Secret characters with whitespace anywhere among them.
_spaced = st.lists(st.one_of(_secret, _hspace), min_size=0, max_size=6).map("".join)
_long = st.tuples(
    st.integers(2049, 5000), st.integers(0, 2049), _hspace, st.booleans()
).map(lambda t: "Q" * t[1] + (t[2] if t[3] else "") + "X" * (t[0] - t[1]))
_password = st.one_of(_spaced, _long)


@st.composite
def _userinfo(draw: st.DrawFn) -> str:
    """Return ``user:password`` whose user already has a non-host character."""
    return "Q_" + draw(_spaced) + ":" + draw(_password)


@st.composite
def _secret_url(draw: st.DrawFn) -> str:
    sep = draw(st.sampled_from(_SEPARATORS))
    url = (
        f"{draw(st.sampled_from(_SCHEMES))}{sep}{draw(_userinfo())}"
        f"@{draw(st.sampled_from(_HOSTS))}{draw(st.sampled_from(_TAILS))}"
    )
    return draw(st.sampled_from(_PREFIXES)) + url + draw(st.sampled_from(_SUFFIXES))


@st.composite
def _split_url(draw: st.DrawFn) -> str:
    """Return two lines: a URL cut inside its userinfo, then the rest of it."""
    scheme = draw(st.sampled_from(_SCHEMES))
    head = f"{draw(st.sampled_from(_PREFIXES))}{scheme}://Q_{draw(_spaced)}:{draw(_secret)}"
    tail = f"{draw(st.sampled_from(('', ' ', '\t')))}{draw(_secret)}@h.example/p rest"
    return head + "\n" + tail


@st.composite
def _host_only(draw: st.DrawFn) -> str:
    """Return a URL without credentials, then a line holding an email address."""
    url = f"{draw(st.sampled_from(_SCHEMES))}://{draw(st.sampled_from(_HOSTS))}"
    glue = draw(st.sampled_from(("\n", " ", "\n\n", "\r\n")))
    return f"see {url}{glue}{_EMAIL}"


@st.composite
def _bare_userinfo(draw: st.DrawFn) -> str:
    """Return ``user:password@host`` with no scheme; the password may hold ``?#``."""
    password = draw(st.text(alphabet=_MARKER_CHARS + "?#", min_size=1, max_size=8))
    return f"{draw(_word)}:{password}@{draw(st.sampled_from(_HOSTS))}"


_lines = st.lists(
    st.one_of(
        _prose.map(lambda s: (False, s)),
        _secret_url().map(lambda s: (True, s)),
        _split_url().map(lambda s: (True, s)),
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
    shown = LogRedactor.redact(_text(lines))
    assert not set(shown) & set(_MARKER_CHARS), shown


@_PROFILE
@given(st.lists(_prose, min_size=1, max_size=8))
def test_every_non_url_line_survives_unchanged(lines: list[str]) -> None:
    text = "\n".join(lines)
    assert LogRedactor.redact(text) == text


@_PROFILE
@given(_host_only())
def test_a_host_only_url_leaves_the_email_after_it_alone(text: str) -> None:
    assert _EMAIL in LogRedactor.redact(text)


@_PROFILE
@given(st.lists(_prose, min_size=0, max_size=3), _split_url(), _prose)
def test_lines_around_a_split_url_survive(
    before: list[str], split: str, after: str
) -> None:
    shown = LogRedactor.redact("\n".join([*before, split, after]))
    assert shown.endswith("\n" + after)
    assert shown.startswith("\n".join(before)) or not before


@_PROFILE
@given(_lines)
def test_redaction_is_idempotent(lines: list[tuple[bool, str]]) -> None:
    once = LogRedactor.redact(_text(lines))
    assert LogRedactor.redact(once) == once


@_PROFILE
@given(_FUZZ)
def test_arbitrary_url_shaped_text_is_idempotent(text: str) -> None:
    once = LogRedactor.redact(text)
    assert LogRedactor.redact(once) == once


# djb's proof inputs that once leaked, kept as explicit examples.  The corpus and
# whitespace tests in test_log_redactor.py hold the rest of p1-p22.
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
)


@pytest.mark.parametrize("text", _PROOFS)
def test_the_proof_inputs_leave_no_secret(text: str) -> None:
    shown = LogRedactor.redact(text)
    assert not set(shown) & set(_MARKER_CHARS)
    assert "SECRET" not in shown
