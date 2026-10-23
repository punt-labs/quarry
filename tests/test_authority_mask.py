"""Tests for quarry.authority_mask: the contract in its docstring, one rule each."""

from __future__ import annotations

import time

import pytest

from quarry.authority_mask import AuthorityMask
from quarry.log_redactor import LogRedactor


class TestOpener:
    @pytest.mark.parametrize(
        "raw",
        [
            "https://u:p@h/x",
            "https:///u:p@h/x",
            "https:/ /u:p@h/x",
            "https : // u:p@h/x",
            "h t t p s://u:p@h/x",
            "HTTP://u:p@h/x",
            "git+ssh://u:p@h/x",
        ],
    )
    def test_the_userinfo_is_masked(self, raw: str) -> None:
        assert "u:p" not in AuthorityMask.mask(raw)
        assert AuthorityMask.mask(raw).endswith("@h/x")

    def test_a_scheme_other_than_the_fetch_ones_is_not_broken_by_whitespace(
        self,
    ) -> None:
        assert AuthorityMask.mask("the s sh://u:p@h/x") == "the s sh://[userinfo]@h/x"

    def test_text_before_the_scheme_is_kept(self) -> None:
        assert AuthorityMask.mask("see (https://u:p@h/x)") == (
            "see (https://[userinfo]@h/x)"
        )


class TestJoiningWithinALine:
    @pytest.mark.parametrize("white", [" ", "\t", "\xa0", "\\x0b", "\\u2028"])
    def test_whitespace_in_a_password_is_joined(self, white: str) -> None:
        shown = AuthorityMask.mask(f"https://u:SEC{white}RET@h/x")
        assert shown == "https://[userinfo]@h/x"

    def test_a_host_only_url_is_not_joined_to_an_address_after_it(self) -> None:
        text = "see http://h.example mail alice@example.com now"
        assert AuthorityMask.mask(text) == text

    def test_a_host_with_a_port_is_not_joined_to_an_address_after_it(self) -> None:
        text = "see http://h.example:8080 mail alice@example.com now"
        assert AuthorityMask.mask(text) == text

    def test_the_run_ends_at_the_first_at_of_a_whitespace_free_token(self) -> None:
        shown = AuthorityMask.mask("https://u:p@h see me@x.com")
        assert shown == "https://[userinfo]@h see me@x.com"

    def test_a_user_holding_an_at_and_a_colon_is_masked_to_the_last_at(self) -> None:
        assert AuthorityMask.mask("http://u@x: SECRETPW@h/p") == (
            "http://[userinfo]@h/p"
        )


class TestAcrossALineBreak:
    def test_the_first_token_of_the_next_line_is_masked(self) -> None:
        shown = AuthorityMask.mask("see https://u:SECRET\npw@h.example/p rest")
        assert shown == "see https://[userinfo]\n[userinfo]@h.example/p rest"

    def test_only_the_first_token_is_masked(self) -> None:
        shown = AuthorityMask.mask("https://u:p\nq@h and me@x.com")
        assert shown.endswith("@h and me@x.com")

    def test_a_host_only_url_leaves_an_address_on_the_next_line_alone(self) -> None:
        text = "see http://h.example\nalice@example.com wrote this"
        assert AuthorityMask.mask(text) == text

    def test_a_url_with_no_authority_yet_takes_the_next_line_as_userinfo(self) -> None:
        shown = AuthorityMask.mask("see https://\nSECRET@h.example/p")
        assert "SECRET" not in shown

    def test_a_credential_cut_before_its_colon_is_found_on_the_next_line(self) -> None:
        shown = AuthorityMask.mask("see https://SECRET\n:pw@h.example/p")
        assert "SECRET" not in shown
        assert "pw" not in shown

    def test_the_line_after_the_continuation_is_left_alone(self) -> None:
        text = "https://u:p\nq@h/x\nbob@example.com"
        assert AuthorityMask.mask(text).endswith("\nbob@example.com")

    @pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
    def test_every_line_break_ends_the_line(self, newline: str) -> None:
        shown = AuthorityMask.mask(f"https://u:SECRET{newline}pw@h/x")
        assert "SECRET" not in shown
        assert newline in shown

    def test_a_url_ending_with_a_path_does_not_continue(self) -> None:
        text = "https://h.example/a:b\nme@x.com"
        assert AuthorityMask.mask(text) == text


class TestPreservation:
    def test_text_without_a_url_is_returned_unchanged(self) -> None:
        text = 'a:b@c\nERROR x: y@z.com\n\n  File "/x.py", line 3'
        assert AuthorityMask.mask(text) == text

    def test_the_line_breaks_are_kept(self) -> None:
        shown = AuthorityMask.mask("a\r\nhttps://u:p@h\n\nb")
        assert shown.split("\n") == ["a\r", "https://[userinfo]@h", "", "b"]

    def test_masking_twice_changes_nothing(self) -> None:
        once = AuthorityMask.mask("https://u:S E@h/x\nhttps://q:r\nz@y/p")
        assert AuthorityMask.mask(once) == once


class TestLinearTime:
    @pytest.mark.parametrize(
        "unit",
        [
            "a: / /",
            "http://a: ",
            "http://" + "a " * 20,
            "h t t p ",
            "x://@ ",
            "http://a@b c/ ",
            "u:p " * 10 + "\n",
        ],
    )
    def test_the_cost_scales_linearly(self, unit: str) -> None:
        def seconds(count: int) -> float:
            started = time.perf_counter()
            AuthorityMask.mask(unit * count)
            return time.perf_counter() - started

        small = min(seconds(1000) for _ in range(3))
        large = min(seconds(4000) for _ in range(3))
        assert large < 8 * max(small, 1e-4)


class TestAHostIsNeverJoinedToWhatFollowsIt:
    """Nothing has userinfo shape yet, so a gap ends the authority (contract 4)."""

    @pytest.mark.parametrize(
        "text",
        [
            "x https://host.com foo:bar@baz.com",
            "GET https://api.io a:b@c.io/x",
            "see http://h.example mail alice@example.com now",
            "https://us er:pw@host.example/p",
        ],
    )
    def test_the_text_after_the_gap_is_left_to_the_scheme_less_pass(
        self, text: str
    ) -> None:
        assert AuthorityMask.mask(text) == text

    @pytest.mark.parametrize(
        ("text", "kept"),
        [
            ("x https://host.com foo:bar@baz.com", "https://host.com/…(segments=0)"),
            ("GET https://api.io a:b@c.io/x", "https://api.io/…(segments=0)"),
        ],
    )
    def test_the_host_is_kept_and_the_separate_credential_masked(
        self, text: str, kept: str
    ) -> None:
        shown = LogRedactor.redact(text)
        assert kept in shown
        assert "[userinfo]@" in shown
        assert "foo:bar" not in shown
        assert "a:b@" not in shown


class TestADigitPasswordSplitAcrossAGap:
    """``name:digits`` reads as host:port until a gap shows it is a password."""

    @pytest.mark.parametrize(
        "text",
        [
            "https://admin:12 34@h.example/x",
            "https://admin:12\n34@h.example",
            "https://admin:12\n34@h.example/x",
            "https://admin:12\t34@h.example",
        ],
    )
    def test_the_rest_of_the_digits_do_not_survive(self, text: str) -> None:
        assert "34" not in LogRedactor.redact(text)

    def test_a_host_with_a_port_is_still_not_joined_to_an_address(self) -> None:
        text = "see http://h.example:8080 mail alice@example.com now"
        assert AuthorityMask.mask(text) == text

    def test_a_host_with_a_port_is_not_joined_to_a_digit_address_on_the_next_line(
        self,
    ) -> None:
        text = "see http://h.example:8080\nalice@example.com wrote"
        assert AuthorityMask.mask(text) == text


class TestTheOpenerBoundary:
    """The scheme is the run of scheme characters before ``:``, of any length."""

    @pytest.mark.parametrize(
        "scheme",
        ["a" * 40, "a" * 33, "-http", "2http", "1.http", "+https", "x-y.z+1"],
    )
    def test_whatever_the_scheme_the_credential_is_masked(self, scheme: str) -> None:
        shown = LogRedactor.redact(f"{scheme}://u:QQ RR@h/x")
        assert "QQ" not in shown
        assert "RR" not in shown
