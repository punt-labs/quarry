"""Tests for quarry.log_redactor: URL secrets and control characters masked."""

from __future__ import annotations

import itertools
import logging
import sys
import time

import pytest

from quarry.log_redaction import RedactingStreamHandler
from quarry.log_redactor import LogRedactor


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
        assert LogRedactor.redact(raw) == shown


def _seconds(text: str) -> float:
    started = time.perf_counter()
    LogRedactor.redact(text)
    return time.perf_counter() - started


class TestLinearTime:
    """A hostile line must not hang the handler (it runs under the lock and GIL).

    Every case is checked as a scaling ratio, never an absolute time: four times
    the input must take well under eight times as long.  The inputs are many
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
        ],
        ids=["colons", "ats", "userinfo-runs", "colon-path", "at-cap", "over-cap"],
    )
    def test_the_cost_scales_linearly(self, unit: str) -> None:
        small = min(_seconds(unit * 25) for _ in range(3))
        large = min(_seconds(unit * 100) for _ in range(3))
        assert large < 8 * max(small, 1e-4)

    def test_a_token_over_two_kilobytes_is_truncated(self) -> None:
        shown = LogRedactor.redact("before " + "q" * 5000 + " after")
        assert shown == "before […] after"

    def test_a_token_of_exactly_the_cap_is_kept(self) -> None:
        token = "q" * LogRedactor.MAX_TOKEN
        assert LogRedactor.redact(token) == token

    def test_a_token_one_over_the_cap_is_truncated(self) -> None:
        assert LogRedactor.redact("q" * (LogRedactor.MAX_TOKEN + 1)) == "[…]"

    def test_a_handler_format_of_a_long_colon_path_url_is_fast(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        url = "http://example.com/" + "a:" * 1000

        def seconds(count: int) -> float:
            record = logging.LogRecord(
                "quarry.x", logging.INFO, __file__, 1, "%s", (url * count,), None
            )
            started = time.perf_counter()
            handler.format(record)
            return time.perf_counter() - started

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
        assert LogRedactor.redact(char) == shown

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
        once = LogRedactor.redact(text)
        assert LogRedactor.redact(once) == once

    def test_the_log_form_of_a_url_is_one_token(self) -> None:
        from quarry.log_form import LogForm

        shown = LogForm.of("https", "h.test", "/a/b")
        assert shown == "https://h.test/…(segments=2)"
        assert len(shown.split()) == 1

    def test_the_log_form_survives_the_redactor_unchanged(self) -> None:
        assert LogRedactor.redact("on https://h.test/…(segments=4) now") == (
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
}


class TestUrlCorpus:
    """Every position a URL can hold a secret, from the djb audit (D1, D2)."""

    @pytest.mark.parametrize("name", list(_URL_CORPUS))
    def test_no_secret_survives(self, name: str) -> None:
        shown = LogRedactor.redact(f"see {_URL_CORPUS[name]} end")
        leaked = [secret for secret in _ALL if secret in shown]
        assert not leaked, f"{leaked} survived in {shown!r}"

    @pytest.mark.parametrize("name", list(_URL_CORPUS))
    def test_redaction_is_idempotent(self, name: str) -> None:
        once = LogRedactor.redact(f"see {_URL_CORPUS[name]} end")
        assert LogRedactor.redact(once) == once

    @pytest.mark.parametrize(
        ("raw", "port_shown"),
        [
            ("https://h.example:8443/a", "https://h.example:8443/…(segments=1)"),
            ("https://h.example:99999/a", "https://h.example:99999/…(segments=1)"),
        ],
    )
    def test_a_numeric_port_is_kept(self, raw: str, port_shown: str) -> None:
        assert LogRedactor.redact(raw) == port_shown

    def test_a_non_numeric_port_is_not_echoed(self) -> None:
        shown = LogRedactor.redact(f"https://h.example:{_PW}/x")
        assert shown == "https://[host]/…(segments=1)"

    def test_a_host_outside_the_grammar_is_not_echoed(self) -> None:
        shown = LogRedactor.redact(f"https://b\u00fccher{_USER}.example/x")
        assert shown == "https://[host]/…(segments=1)"

    def test_an_ipv6_host_is_kept(self) -> None:
        shown = LogRedactor.redact("https://u:p@[2001:db8::1]:8443/a/b")
        assert shown == "https://[2001:db8::1]:8443/…(segments=2)"


class TestRecordCap:
    """The whole record is bounded before any pattern runs (D8)."""

    def test_a_record_over_the_cap_is_truncated_with_a_visible_marker(self) -> None:
        cap = LogRedactor.MAX_RECORD
        shown = LogRedactor.redact("a " * cap)
        assert shown.endswith(f"[…truncated {2 * cap - cap} bytes]")
        assert len(shown) < cap + 40

    def test_a_record_at_the_cap_is_kept_whole(self) -> None:
        text = "w" * 10 + (" x" * ((LogRedactor.MAX_RECORD - 10) // 2))
        text = text[: LogRedactor.MAX_RECORD]
        assert LogRedactor.redact(text) == text

    def test_a_secret_beyond_the_cap_is_never_kept(self) -> None:
        text = "x " * LogRedactor.MAX_RECORD + "https://u:SECRET@h/p"
        assert "SECRET" not in LogRedactor.redact(text)

    def test_cost_is_bounded_by_the_cap_not_the_record(self) -> None:
        small = min(_seconds("w " * (LogRedactor.MAX_RECORD // 2)) for _ in range(3))
        huge = min(_seconds("w " * (8 * LogRedactor.MAX_RECORD)) for _ in range(3))
        assert huge < 4 * max(small, 1e-4)

    def test_text_with_no_url_characters_costs_less_than_a_url_laden_line(self) -> None:
        plain = min(_seconds("word " * 12_000) for _ in range(3))
        urls = min(_seconds("https://u:p@h.test/a/b?q=1 " * 2_200) for _ in range(3))
        assert plain < urls


_WS = ("\n", "\t", "\r")
_SPLITS = {
    "mid-scheme": "ht{w}tps://user:SECRETPW@host.example/SECRETPATH",
    "before-colon": "https{w}://user:SECRETPW@host.example/SECRETPATH",
    "mid-separator": "https:/{w}/user:SECRETPW@host.example/SECRETPATH",
    "after-colon": "https:{w}//user:SECRETPW@host.example/SECRETPATH",
    "mid-user": "https://us{w}er:SECRETPW@host.example/SECRETPATH",
    "mid-password": "https://user:SEC{w}RETPW@host.example/SECRETPATH",
    "before-at": "https://user:SECRETPW{w}@host.example/SECRETPATH",
    "mid-host": "https://user:SECRETPW@host.{w}example/SECRETPATH",
    "mid-path": "https://host.example/SECRET{w}PATH",
    "mid-query": "https://host.example/p?tok=SECR{w}ETQ",
}


class TestWhitespaceInsideAUrl:
    """urlsplit deletes tab, CR and LF anywhere in a URL; so must the redactor."""

    @pytest.mark.parametrize("where", list(_SPLITS))
    @pytest.mark.parametrize("white", _WS, ids=["lf", "tab", "cr"])
    def test_no_secret_survives(self, where: str, white: str) -> None:
        raw = _SPLITS[where].format(w=white)
        shown = LogRedactor.redact(f"see {raw} end")
        for secret in ("SECRETPW", "SECRETPATH", "SECRETQ", "SECR", "ETQ", "RETPW"):
            assert secret not in shown, f"{secret} survived: {shown!r}"

    @pytest.mark.parametrize("where", list(_SPLITS))
    def test_redaction_is_idempotent(self, where: str) -> None:
        once = LogRedactor.redact("see " + _SPLITS[where].format(w="\n") + " end")
        assert LogRedactor.redact(once) == once

    def test_text_that_is_not_a_url_keeps_its_line_breaks(self) -> None:
        assert LogRedactor.redact("a\tb\nc: d//e") == "a\tb\nc: d//e"

    def test_a_deterministic_sweep_finds_no_leak_and_stays_idempotent(self) -> None:
        """djb's p13 fuzz as a fixed sweep: every combination, a break at each spot."""
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
            userinfo = userinfo[:at] + _WS[n % 3] + userinfo[at:]
            text = wrap % f"{scheme}://{userinfo}@{host}{tail}"
            out = LogRedactor.redact(text)
            if "SECRET" in out or "SEC.RET" in out or "S%40" in out:
                leaks.append(text)
            if LogRedactor.redact(out) != out:
                unstable.append(text)
        assert leaks == []
        assert unstable == []
