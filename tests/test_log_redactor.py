"""Tests for quarry.log_redactor: URL secrets and control characters masked."""

from __future__ import annotations

import pytest

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
