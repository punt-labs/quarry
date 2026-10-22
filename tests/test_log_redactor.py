"""Tests for quarry.log_redactor: URL secrets and control characters masked."""

from __future__ import annotations

import logging
import sys
import time

import pytest

from quarry.http_url import HttpUrl
from quarry.log_redaction import RedactingStreamHandler
from quarry.log_redactor import LogRedactor


class TestRedact:
    @pytest.mark.parametrize(
        ("raw", "shown"),
        [
            (
                "GET https://u:SECRET@h.test/p?token=SECRET#f ok",
                "GET https://h.test/p ok",
            ),
            ("see http://h.test:8080/a/b?x=1&y=2", "see http://h.test:8080/a/b"),
            ("at ftp://user@h.test/x", "at ftp://h.test/x"),
            ("opened u:SECRET@h.test/p", "opened [userinfo]@h.test/p"),
            ("bare h.test/p?token=SECRET end", "bare h.test/p?[query] end"),
            ("a\x1b[31mred\x07 b", "a\\x1b[31mred\\x07 b"),
            ("line\x85next\u2028sep", "line\\x85next\\x2028sep"),
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


class TestLinearTime:
    """A hostile token must not hang the handler (it runs under the lock and GIL)."""

    @pytest.mark.parametrize(
        "text",
        ["u:" * 20_000, "a" * 200_000, "http://h/" + "a:" * 20_000, "x@" * 20_000],
        ids=["colons", "plain", "colon-path", "ats"],
    )
    def test_redact_finishes_quickly(self, text: str) -> None:
        started = time.perf_counter()
        LogRedactor.redact(text)
        assert time.perf_counter() - started < 0.1

    def test_a_token_over_two_kilobytes_is_truncated(self) -> None:
        shown = LogRedactor.redact("before " + "q" * 5000 + " after")
        assert shown == "before […] after"

    def test_a_handler_emit_of_a_long_colon_path_url_is_fast(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        url = HttpUrl("http://example.com/" + "a:" * 2000)
        record = logging.LogRecord(
            "quarry.x", logging.INFO, __file__, 1, "%s", (url,), None
        )
        started = time.perf_counter()
        handler.format(record)
        assert time.perf_counter() - started < 0.1


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
        assert shown == "name\\n2026-01-01 [ERROR] forged\\r\\x202egnirts"

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
