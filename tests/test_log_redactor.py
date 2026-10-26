"""Tests for quarry.log_redactor: URL secrets and control characters masked."""

from __future__ import annotations

import itertools
import logging
import sys
import time

import pytest

from quarry.log_redaction import RedactingStreamHandler
from quarry.log_redactor import LogRedactor
from tests.redaction import redacted


class TestRedact:
    @pytest.mark.parametrize(
        ("raw", "shown"),
        [
            (
                "GET https://u:SECRET@h.test/p?token=SECRET#f ok",
                "GET https://h.test/…(segments=1) ok",
            ),
            (
                "see http://h.test:8080/a/b?x=1&y=2",
                "see http://h.test:8080/…(segments=2)",
            ),
            ("at ftp://user@h.test/x", "at ftp://h.test/…(segments=1)"),
            ("opened u:SECRET@h.test/p", "opened [userinfo]@h.test/p"),
            ("bare h.test/p?token=SECRET end", "bare h.test/p?[query] end"),
            ("a\x1b[31mred\x07 b", "a\\x1b[31mred\\x07 b"),
            ("line\x85next\u2028sep", "line\\x85next\\u2028sep"),
            ("plain text: nothing to hide?", "plain text: nothing to hide?"),
            ("tab\tand newline\nstay", "tab\tand newline\nstay"),
        ],
        ids=[
            "scheme-userinfo-query-fragment",
            "scheme-query",
            "scheme-userinfo-only",
            "bare-userinfo",
            "bare-query",
            "escape-control",
            "c1-and-separator",
            "plain",
            "tab-newline-kept",
        ],
    )
    def test_redact(self, raw: str, shown: str) -> None:
        assert redacted(raw) == shown


def _seconds(text: str) -> float:
    started = time.process_time()
    redacted(text)
    return time.process_time() - started


class TestLinearTime:
    """A hostile line must not hang the handler (it runs under the lock and GIL).

    Time is this process's CPU time (``time.process_time``), not wall time, so a
    loaded machine waiting for the CPU does not count against it.  Every case but
    one is checked as a scaling ratio: four times the input must take well under
    eight times as long.  The inputs are many
    tokens each UNDER the cap, because one token over it is truncated before any
    pattern runs and so proves nothing.
    """

    @pytest.mark.parametrize(
        "unit",
        [
            "u:" * 1000 + " ",
            "x@" * 1000 + " ",
            ("a" * 127 + ":") * 15 + " ",
            "http://h/" + "a:" * 1000 + " ",
            "q" * 2048 + " ",
            "a" * 2049 + " ",
            "a" * 2000 + "@ ",
            ("a" * 700 + ":") * 2 + "b" * 600 + "@ ",
            "://" + "a" * 2045 + " ",
            "a" * 2044 + "://" + "a" * 2 + " ",
        ],
        ids=[
            "colons",
            "ats",
            "userinfo-runs",
            "colon-path",
            "at-cap",
            "over-cap",
            "long-user-run",
            "colon-separated-runs",
            "slashes-then-long-run",
            "long-scheme-run",
        ],
    )
    def test_the_cost_scales_linearly(self, unit: str) -> None:
        small = min(_seconds(unit * 25) for _ in range(3))
        large = min(_seconds(unit * 100) for _ in range(3))
        assert large < 8 * max(small, 1e-4)

    @pytest.mark.parametrize(
        "token",
        ["://" + "a" * 2045, "://" + "a" * 2040 + ":/x", "a" * 2000 + "://" + "b" * 40],
        ids=["slashes-then-run", "run-with-colon-slash", "run-then-slashes"],
    )
    def test_a_token_of_scheme_characters_is_not_quadratic(self, token: str) -> None:
        """Each token is under the cap, so the cost per token is the cost per byte.

        A scheme pattern that is retried at every offset of a 2 KB run costs
        milliseconds per token (about 0.3 s for a 64 KiB record); anchored at the
        start of the run it costs microseconds.
        """
        record = " ".join([token] * (LogRedactor.MAX_RECORD // len(token)))
        assert min(_seconds(record) for _ in range(3)) < 0.1

    def test_a_token_over_two_kilobytes_is_truncated(self) -> None:
        shown = redacted("before " + "q" * 5000 + " after")
        assert shown == "before […] after"

    def test_a_token_of_exactly_the_cap_is_kept(self) -> None:
        token = "q" * LogRedactor.MAX_TOKEN
        assert redacted(token) == token

    def test_a_token_one_over_the_cap_is_truncated(self) -> None:
        assert redacted("q" * (LogRedactor.MAX_TOKEN + 1)) == "[…]"

    def test_a_handler_format_of_a_long_colon_path_url_is_fast(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        url = "http://example.com/" + "a:" * 1000

        def seconds(count: int) -> float:
            record = logging.LogRecord(
                "quarry.x", logging.INFO, __file__, 1, "%s", (url * count,), None
            )
            started = time.process_time()
            handler.format(record)
            return time.process_time() - started

        small = min(seconds(25) for _ in range(3))
        large = min(seconds(100) for _ in range(3))
        assert large < 8 * max(small, 1e-4)


class TestLineForging:
    def test_newlines_and_bidi_controls_in_the_message_are_escaped(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        message = "name\n2026-01-01 [ERROR] forged\r\u202egnirts"
        record = logging.LogRecord(
            "quarry.x", logging.INFO, __file__, 1, message, (), None
        )
        shown = handler.format(record)
        assert "\n" not in shown
        assert "\r" not in shown
        assert "\u202e" not in shown
        assert shown == "name\\n2026-01-01 [ERROR] forged\\r\\u202egnirts"

    def test_traceback_newlines_are_kept(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = _record_with_traceback()
        assert handler.format(record).count("\n") >= 2


def _record_with_traceback() -> logging.LogRecord:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        return logging.LogRecord(
            "quarry.x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )


class TestEscapes:
    @pytest.mark.parametrize(
        ("char", "shown"),
        [
            ("\x07", "\\x07"),
            ("\x85", "\\x85"),
            ("\u2028", "\\u2028"),
            ("\U0001f600", "\U0001f600"),
        ],
        ids=["ascii", "c1", "bmp", "astral-untouched"],
    )
    def test_a_control_character_is_escaped_unambiguously(
        self, char: str, shown: str
    ) -> None:
        assert redacted(char) == shown

    @pytest.mark.parametrize(
        "char",
        [chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))],
    )
    def test_every_bidi_control_is_escaped_in_a_message(self, char: str) -> None:
        shown = LogRedactor.one_line(f"a{char}b")
        assert shown == f"a\\u{ord(char):04x}b"

    def test_a_newline_arriving_through_an_argument_is_escaped(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            "quarry.x", logging.INFO, __file__, 1, "name %s", ("a\nb",), None
        )
        assert handler.format(record) == "name a\\nb"


_FORMS = [
    "see https://u:p@h.test/a/b?q=1#f ok",
    "(http://h.test:8080/a/b)",
    "x https://h.test/…(segments=3) y",
    "ftp://user@h.test/x and bare u:pw@h.test/p?token=T",
    "a\x1b[31mb https://h.test/ c",
    "https://h.test/…(segments=2)/leftover",
    "(https://h.test/…(segments=3)), done",
]


class TestIdempotence:
    """A redacted line is a fixed point: the log form is not rewritten again."""

    @pytest.mark.parametrize("text", _FORMS)
    def test_redacting_twice_equals_redacting_once(self, text: str) -> None:
        once = redacted(text)
        assert redacted(once) == once

    def test_the_log_form_of_a_url_is_one_token(self) -> None:
        from quarry.log_form import LogForm

        shown = LogForm.of("https", "h.test", "/a/b")
        assert shown == "https://h.test/…(segments=2)"
        assert len(shown.split()) == 1

    def test_the_log_form_survives_the_redactor_unchanged(self) -> None:
        assert redacted("on https://h.test/…(segments=4) now") == (
            "on https://h.test/…(segments=4) now"
        )


_USER, _PW, _PATH, _QUERY, _FRAG = (
    "SECRETUSER",
    "SECRETPW",
    "SECRETPATH",
    "SECRETQ",
    "SECRETFRAG",
)
_ALL = (_USER, _PW, _PATH, _QUERY, _FRAG)
# A secret in these positions of a scheme URL must never survive redaction.
_URL_CORPUS = {
    "plain": f"https://{_USER}:{_PW}@h.example/{_PATH}/x?k={_QUERY}#{_FRAG}",
    "paren": f"(https://{_USER}:{_PW}@h.example/{_PATH}/x?k={_QUERY})",
    "angle": f"<https://{_USER}:{_PW}@h.example/{_PATH}>",
    "squote": f"'https://{_USER}:{_PW}@h.example/{_PATH}'",
    "dquote": f'"https://{_USER}:{_PW}@h.example/{_PATH}"',
    "json": f'{{"url":"https://{_USER}:{_PW}@h.example/{_PATH}?a={_QUERY}"}}',
    "kv": f"url=https://{_USER}:{_PW}@h.example/{_PATH}",
    "trail": f"https://{_USER}:{_PW}@h.example/{_PATH}.",
    "repr": f"URL('https://{_USER}:{_PW}@h.example:8443/{_PATH}')",
    "ipv6": f"https://{_USER}:{_PW}@[2001:db8::1]:8443/{_PATH}?x={_QUERY}",
    "pct userinfo": f"https://{_USER}%40x:{_PW}%2F@h.example/{_PATH}",
    "idna": f"https://{_USER}:{_PW}@b\u00fccher.example/{_PATH}",
    "multi at": f"https://{_USER}:{_PW}@a@h.example/{_PATH}",
    "user only": f"https://{_USER}@h.example/{_PATH}",
    "ftp": f"ftp://{_USER}:{_PW}@h.example/{_PATH}",
    "wss": f"wss://{_USER}:{_PW}@h.example/{_PATH}",
    "nbsp split": f"https://{_USER}:{_PW}\u00a0@h.example/{_PATH}",
    "newline split": f"https://{_USER}:{_PW}\n@h.example/{_PATH}",
    "tab split": f"https://{_USER}:{_PW}\t@h.example/{_PATH}",
    "uppercase": f"HTTPS://{_USER}:{_PW}@H.EXAMPLE/{_PATH}",
    "backslash": f"https://h.example\\@{_USER}/{_PATH}",
    "userinfo 300": f"https://{_USER * 30}:x@h.example/{_PATH}",
    "password 300": f"https://u:{_PW * 40}@h.example/{_PATH}",
    "query only": f"https://h.example?{_QUERY}=1",
    "fragment only": f"https://h.example#{_FRAG}",
    "glued urls": f"https://a.example/{_PATH},https://{_USER}:{_PW}@b.example/{_PATH}",
    "colon after host": f"https://h.example:{_PW}/x/{_PATH}",
    "long authority": f"https://{'a' * 300}:{_PW}@h.example/{_PATH}",
    "long jwt user": f"https://eyJ{'A' * 300}@h.example/{_PATH}",
    "quote in password": f'GET https://{_USER}:p"{_PW}@h.example/{_PATH}?x=1',
    "apostrophe in password": f"GET https://{_USER}:p'{_PW}@h.example/{_PATH}",
    "angle in password": f"GET https://{_USER}:p<{_PW}@h.example/{_PATH}",
    "apostrophe in path": f"see https://h.example/o'brien/cb#{_FRAG} end",
    "quote in path": f'see https://h.example/a"b/cb#{_FRAG} end',
    "backtick in path": f"see https://h.example/a`b/cb#{_FRAG} end",
    "apostrophe then query": f"see https://h.example/o'brien?k={_QUERY} end",
}


class TestUrlCorpus:
    """Every position a URL can hold a secret."""

    @pytest.mark.parametrize("name", list(_URL_CORPUS))
    def test_no_secret_survives(self, name: str) -> None:
        shown = redacted(f"see {_URL_CORPUS[name]} end")
        # A line break ends the URL: the rest of the next line is kept as it is.
        secrets = (_USER, _PW) if name == "newline split" else _ALL
        leaked = [secret for secret in secrets if secret in shown]
        assert not leaked, f"{leaked} survived in {shown!r}"

    @pytest.mark.parametrize("name", list(_URL_CORPUS))
    def test_redaction_is_idempotent(self, name: str) -> None:
        once = redacted(f"see {_URL_CORPUS[name]} end")
        assert redacted(once) == once

    @pytest.mark.parametrize(
        ("raw", "port_shown"),
        [
            ("https://h.example:8443/a", "https://h.example:8443/…(segments=1)"),
            ("https://h.example:99999/a", "https://h.example:99999/…(segments=1)"),
        ],
    )
    def test_a_numeric_port_is_kept(self, raw: str, port_shown: str) -> None:
        assert redacted(raw) == port_shown

    def test_a_non_numeric_port_is_not_echoed(self) -> None:
        shown = redacted(f"https://h.example:{_PW}/x")
        assert shown == "https://[host]/…(segments=1)"

    def test_a_host_outside_the_grammar_is_not_echoed(self) -> None:
        shown = redacted(f"https://b\u00fccher{_USER}.example/x")
        assert shown == "https://[host]/…(segments=1)"

    def test_an_ipv6_host_is_kept(self) -> None:
        shown = redacted("https://u:p@[2001:db8::1]:8443/a/b")
        assert shown == "https://[2001:db8::1]:8443/…(segments=2)"


class TestRecordCap:
    """The whole record is bounded before any pattern runs (D8)."""

    def test_a_record_over_the_cap_is_truncated_with_a_visible_marker(self) -> None:
        cap = LogRedactor.MAX_RECORD
        shown = redacted("a " * cap)
        assert shown.endswith(f"[…truncated {2 * cap - cap} bytes]")
        assert len(shown) < cap + 40

    def test_a_record_at_the_cap_is_kept_whole(self) -> None:
        text = "w" * 10 + (" x" * ((LogRedactor.MAX_RECORD - 10) // 2))
        text = text[: LogRedactor.MAX_RECORD]
        assert redacted(text) == text

    def test_a_secret_beyond_the_cap_is_never_kept(self) -> None:
        text = "x " * LogRedactor.MAX_RECORD + "https://u:SECRET@h/p"
        assert "SECRET" not in redacted(text)

    def test_cost_is_bounded_by_the_cap_not_the_record(self) -> None:
        small = min(_seconds("w " * (LogRedactor.MAX_RECORD // 2)) for _ in range(3))
        huge = min(_seconds("w " * (8 * LogRedactor.MAX_RECORD)) for _ in range(3))
        assert huge < 4 * max(small, 1e-4)

    def test_text_with_no_url_characters_costs_less_than_a_url_laden_line(self) -> None:
        plain = min(_seconds("word " * 12_000) for _ in range(3))
        urls = min(_seconds("https://u:p@h.test/a/b?q=1 " * 2_200) for _ in range(3))
        assert plain < urls


_WS = ("\n", "\t", "\r")
# Every whitespace character urlsplit or a log reader may put inside a URL.
_ALL_WS = (
    "\n",
    "\t",
    "\r",
    " ",
    "\x0b",
    "\x0c",
    "\x1c",
    "\x85",
    "\xa0",
    "\u2028",
    "\u2029",
)
_SPLITS = {
    "mid-scheme": "ht{w}tps://user:SECRETPW@host.example/SECRETPATH",
    "before-colon": "https{w}://user:SECRETPW@host.example/SECRETPATH",
    "mid-separator": "https:/{w}/user:SECRETPW@host.example/SECRETPATH",
    "after-colon": "https:{w}//user:SECRETPW@host.example/SECRETPATH",
    "mid-user": "https://us{w}er:SECRETPW@host.example/SECRETPATH",
    "mid-password": "https://user:SEC{w}RETPW@host.example/SECRETPATH",
    "before-at": "https://user:SECRETPW{w}@host.example/SECRETPATH",
    "after-at": "https://user:SECRETPW@{w}host.example/SECRETPATH",
    "mid-host": "https://user:SECRETPW@host.{w}example/SECRETPATH",
}


class TestWhitespaceInsideAUrl:
    """Whitespace inside the scheme, the separator and the userinfo is joined.

    urlsplit deletes tab, CR and LF anywhere in a URL, so a credential can be
    cut anywhere.  What the contract in AuthorityMask guarantees is that no
    credential survives; the path and the host are text of their own once a line
    break or a break in the host separates them from the credential.
    """

    @pytest.mark.parametrize("where", list(_SPLITS))
    @pytest.mark.parametrize("white", _ALL_WS, ids=lambda w: f"U+{ord(w):04X}")
    def test_no_secret_survives(self, where: str, white: str) -> None:
        raw = _SPLITS[where].format(w=white)
        shown = redacted(f"see {raw} end")
        # The path is not a credential.  It is dropped only while the URL is one
        # unbroken run: a line break ends the URL, and a break in the host leaves
        # the host and path as text of their own.
        keeps_path = white in "\n\r" or where in ("after-at", "mid-host", "mid-user")
        secrets = ("SECRETPW", "SEC", "RETPW") + (() if keeps_path else ("SECRETPATH",))
        checked = shown.replace("SECRETPATH", "") if keeps_path else shown
        for secret in secrets:
            assert secret not in checked, f"{secret} survived: {shown!r}"

    @pytest.mark.parametrize("where", list(_SPLITS))
    @pytest.mark.parametrize("white", _ALL_WS, ids=lambda w: f"U+{ord(w):04X}")
    def test_redaction_is_idempotent(self, where: str, white: str) -> None:
        once = redacted("see " + _SPLITS[where].format(w=white) + " end")
        assert redacted(once) == once

    @pytest.mark.parametrize(
        "following",
        [
            "Traceback (most recent call last):",
            "ERROR x failed",
            '  File "/x.py", line 3, in f',
            "\nThe above exception was the direct cause",
            "quarry.fetch: next record",
        ],
        ids=["traceback", "error", "file", "blank-line", "logger"],
    )
    @pytest.mark.parametrize(
        "url", ["https://h.example/a/b", "https://u:p@h.example/a"]
    )
    def test_a_url_ending_a_line_leaves_the_next_line_alone(
        self, url: str, following: str
    ) -> None:
        shown = redacted(f"see {url}\n{following}")
        assert shown.endswith(f"\n{following}")

    def test_a_url_ending_a_line_with_credentials_and_no_path_survives_too(
        self,
    ) -> None:
        shown = redacted("see https://u:SECRETPW@h.example\nTraceback (most")
        assert "SECRETPW" not in shown
        assert shown.endswith("\nTraceback (most")

    def test_a_break_in_the_path_ends_the_url_and_the_rest_is_left_alone(self) -> None:
        shown = redacted("https://h.example/a/b\nrest of line")
        assert shown == "https://h.example/…(segments=2)\nrest of line"

    def test_text_that_is_not_a_url_keeps_its_line_breaks(self) -> None:
        assert redacted("a\tb\nc: d//e") == "a\tb\nc: d//e"

    def test_a_deterministic_sweep_finds_no_leak_and_stays_idempotent(self) -> None:
        """A fixed sweep of the fuzz cases, with every whitespace character."""
        schemes = ["http", "HtTp", "ftp", "a+b.c-d"]
        users = ["user", "", "us:er", "u@x"]
        passwords = ["SECRETPW", "S%40ECRET", "SEC.RET", "SECRETPW" * 20]
        hosts = ["h.example", "[::1]:80", "h:80", "h@h"]
        tails = ["", "/p/q?x=1", "#f", "/p@q"]
        wraps = ["%s", "(%s)", "<%s>", "'%s'", 'x="%s"', "%s,"]
        leaks: list[str] = []
        unstable: list[str] = []
        combos = itertools.product(schemes, users, passwords, hosts, tails, wraps)
        for n, (scheme, user, password, host, tail, wrap) in enumerate(combos):
            userinfo = user + ":" + password
            at = n % (len(userinfo) + 1)
            userinfo = userinfo[:at] + _ALL_WS[n % len(_ALL_WS)] + userinfo[at:]
            text = wrap % f"{scheme}://{userinfo}@{host}{tail}"
            out = redacted(text)
            if "SECRET" in out or "SEC.RET" in out or "S%40" in out:
                leaks.append(text)
            if redacted(out) != out:
                unstable.append(text)
        assert leaks == []
        assert unstable == []


class TestTruncationMarker:
    def test_a_single_run_over_the_record_cap_still_shows_the_marker(self) -> None:
        cap = LogRedactor.MAX_RECORD
        shown = redacted("x" * (cap + 4_000))
        assert shown.endswith(" […truncated 4000 bytes]")
        assert shown.startswith("[…]")

    def test_the_marker_is_separate_from_the_kept_text(self) -> None:
        cap = LogRedactor.MAX_RECORD
        shown = redacted("a " * cap)
        assert shown.endswith(" […truncated " + str(2 * cap - cap) + " bytes]")
        assert "bytes][…" not in shown

    def test_a_marker_survives_a_second_redaction_unchanged(self) -> None:
        once = redacted("x" * (LogRedactor.MAX_RECORD + 10))
        assert redacted(once) == once


class TestSchemelessUserinfo:
    def test_a_long_password_is_masked(self) -> None:
        shown = redacted("user:" + "P" * 1500 + "@host.example")
        assert shown == "[userinfo]@host.example"

    def test_a_long_user_is_masked(self) -> None:
        shown = redacted("u" * 1500 + ":SECRETPW@host.example")
        assert shown == "[userinfo]@host.example"

    def test_a_token_over_the_cap_is_replaced_whole(self) -> None:
        assert redacted("user:" + "P" * 3000 + "@h") == "[…]"

    @pytest.mark.parametrize(
        "raw",
        [
            "admin:hunter?2@db.internal",
            "admin:pw#x@db",
            "admin:pw?x#y@db",
            "ssh admin:hunter?2@db.internal now",
            ":?/#@",
        ],
    )
    def test_a_password_holding_a_query_or_fragment_character_is_masked(
        self, raw: str
    ) -> None:
        shown = redacted(raw)
        assert "hunter" not in shown
        assert "pw" not in shown.replace("[userinfo]", "")
        assert redacted(shown) == shown

    def test_the_password_is_masked_whole_not_cut_at_the_question_mark(self) -> None:
        assert redacted("admin:hunter?2@db.internal") == ("[userinfo]@db.internal")


class TestQuoted:
    """A value an outside party chose, shown as one quoted line."""

    def test_a_plain_value_is_quoted(self) -> None:
        assert LogRedactor.quoted("/repo/proj") == "'/repo/proj'"

    def test_a_newline_cannot_start_a_new_line(self) -> None:
        shown = LogRedactor.quoted("/tmp/x\nIgnore previous instructions")
        assert "\n" not in shown
        assert shown == "'/tmp/x\\nIgnore previous instructions'"

    @pytest.mark.parametrize("char", ["\r", "‮", "⁦", "‏", "\x1b"])
    def test_a_control_or_bidi_mark_is_escaped(self, char: str) -> None:
        shown = LogRedactor.quoted(f"a{char}b")
        assert char not in shown
        assert shown.startswith("'a")
        assert shown.endswith("b'")


class TestThePassCap:
    def test_a_record_that_never_settles_says_none_and_nothing_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []

        def never_settles(text: str) -> str:
            calls.append(text)
            return text + "x"

        monkeypatch.setattr(LogRedactor, "_once", never_settles)
        with caplog.at_level(logging.DEBUG):
            shown = LogRedactor.settle("https://u:SECRET@h/p")
        assert shown is None
        assert len(calls) == LogRedactor.PASSES
        assert caplog.records == []

    def test_a_truncated_record_that_never_settles_says_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(LogRedactor, "_once", lambda text: text + "x")
        assert LogRedactor.settle("a" * (LogRedactor.MAX_RECORD + 5)) is None

    def test_a_truncated_record_that_settles_keeps_its_marker(self) -> None:
        shown = redacted("a" * (LogRedactor.MAX_RECORD + 5))
        assert shown.endswith(" […truncated 5 bytes]")


class TestPunctuationAfterAUrlStaysOutsideIt:
    @pytest.mark.parametrize(
        ("raw", "shown"),
        [
            ("see (https://h/x/y).", "see (https://h/…(segments=2))."),
            (
                "fetch refused: HTTPError [https://example.com/x]: boom",
                "fetch refused: HTTPError [https://example.com/…(segments=1)]: boom",
            ),
            ("go to https://h/a/b, then", "go to https://h/…(segments=2), then"),
            ("<https://h/a>", "<https://h/…(segments=1)>"),
            ("said 'https://h/a'.", "said 'https://h/…(segments=1)'."),
            ("https://h/a;", "https://h/…(segments=1);"),
            ("https://h/p?x=1).", "https://h/…(segments=1)"),
            ("https://h:8080.", "https://h:8080/…(segments=0)."),
        ],
        ids=[
            "paren-period",
            "bracket-colon",
            "comma",
            "angle",
            "quotes",
            "semicolon",
            "query-then-paren",
            "host-period",
        ],
    )
    def test_the_delimiters_follow_the_log_form(self, raw: str, shown: str) -> None:
        assert redacted(raw) == shown

    def test_a_balanced_pair_inside_the_url_is_part_of_it(self) -> None:
        assert redacted("https://h/a_(b)/c.") == "https://h/…(segments=2)."

    def test_a_delimiter_run_cannot_hide_userinfo(self) -> None:
        shown = redacted("https://u:SECRET@h/p).")
        assert "SECRET" not in shown
        assert shown == "https://h/…(segments=1))."


class TestTheTailNeverRevealsAQueryOrAHostPosition:
    def test_delimiters_after_a_query_are_dropped(self) -> None:
        assert redacted("http://h/p?t=ab;;;;") == "http://h/…(segments=1)"

    @pytest.mark.parametrize("char", ["'", '"', "<", ">"])
    @pytest.mark.parametrize("opener", ["?t=", "#"])
    def test_a_quote_or_angle_bracket_inside_a_query_does_not_end_the_match(
        self, char: str, opener: str
    ) -> None:
        shown = redacted(f"http://h/p{opener}AB{char}CDSECRET")
        assert shown == "http://h/…(segments=1)"

    def test_a_quote_still_ends_the_url_in_the_path(self) -> None:
        assert redacted("say 'http://h/a' now") == "say 'http://h/…(segments=1)' now"

    @pytest.mark.parametrize("char", ["'", '"', "<", ">", "#"])
    @pytest.mark.parametrize("shape", ["GET /p?{}", "host/p?{}"])
    def test_a_scheme_less_query_runs_to_whitespace(
        self, char: str, shape: str
    ) -> None:
        shown = redacted(shape.format(f"a{char}bSECRET") + " end")
        assert shown == shape.format("[query]") + " end"

    @pytest.mark.parametrize(
        "text",
        [
            "GET h:80?t=QQSE@CRETZZ]",
            "h.example:8080?x@SECRET",
            "GET [::1]:80?t=Q@SECRET",
        ],
    )
    def test_a_query_after_host_and_port_is_masked_whole_even_with_an_at_sign(
        self, text: str
    ) -> None:
        shown = redacted(text)
        assert "SECRET" not in shown
        assert "CRET" not in shown
        assert "?[query]" in shown

    @pytest.mark.parametrize(
        "text", ["user:80?rest@host", "GET user:12345?Q@h.example end", "u:1?x@h"]
    )
    def test_both_readings_are_masked_when_an_at_sign_follows_the_query(
        self, text: str
    ) -> None:
        shown = redacted(text)
        assert "[userinfo]?[query]" in shown
        assert not any(char.isdigit() for char in shown)

    def test_a_host_and_port_with_a_plain_query_keeps_its_host(self) -> None:
        assert (
            redacted("GET h.example:8080?x=1 end") == "GET h.example:8080?[query] end"
        )

    def test_a_password_that_holds_a_question_mark_is_still_userinfo(self) -> None:
        assert redacted("admin:Q?X@db.internal") == "[userinfo]@db.internal"

    def test_the_query_placeholder_is_not_matched_again(self) -> None:
        assert redacted("GET /p?[query]") == "GET /p?[query]"

    def test_delimiters_after_a_fragment_are_dropped(self) -> None:
        assert redacted("http://h/p#ab.,;") == "http://h/…(segments=1)"

    def test_a_closer_that_closes_a_bracket_opened_in_the_token_is_kept(self) -> None:
        assert redacted("(http://h/p?t=ab)") == "(http://h/…(segments=1))"

    def test_a_closer_is_kept_after_dropped_delimiters(self) -> None:
        assert redacted("(http://h/p?t=ab;.)") == "(http://h/…(segments=1))"

    @pytest.mark.parametrize(
        ("text", "shown"),
        [
            ("http://h/p?t=ab)", "http://h/…(segments=1)"),
            ("(http://h/p?t=ab))", "(http://h/…(segments=1))"),
            ("[http://h/p#f])", "[http://h/…(segments=1)]"),
            ("http://h/p?t=ab])", "http://h/…(segments=1)"),
            ("(see http://h/p?t=ab)", "(see http://h/…(segments=1)"),
        ],
        ids=["none-open", "one-open-two-close", "bracket", "mixed", "other-token"],
    )
    def test_no_more_closers_are_kept_than_openers_precede_the_url(
        self, text: str, shown: str
    ) -> None:
        assert redacted(text) == shown

    @pytest.mark.parametrize(
        "text", ["https://X:", "see https://X: ok", "https://abc:def:"]
    )
    def test_a_colon_after_a_bare_host_keeps_the_host_position_masked(
        self, text: str
    ) -> None:
        assert "[host]" in redacted(text)

    @pytest.mark.parametrize(
        ("text", "shown"),
        [
            ("x https://user:pss@h.com: end", "x https://[userinfo]/…(segments=0)"),
            # a credentialed URL's trailing punctuation is over-redacted, never kept
            ("(w s s:/ /QQAAA.BBBZZ@-)", "(wss://[userinfo]/…(segments=0)"),
            ("https://u:QQ;@h.", "https://h/…(segments=0)."),
        ],
    )
    def test_credentials_before_punctuation_are_masked(
        self, text: str, shown: str
    ) -> None:
        assert redacted(text) == shown
