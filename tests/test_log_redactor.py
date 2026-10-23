"""Tests for quarry.log_redactor: URL secrets and control characters masked."""

from __future__ import annotations

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
