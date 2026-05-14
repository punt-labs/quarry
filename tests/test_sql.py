"""Tests for the _sql shared helper module."""

from __future__ import annotations

from quarry._sql import escape_sql


class TestEscapeSql:
    def test_no_quotes(self):
        assert escape_sql("hello") == "hello"

    def test_single_quote(self):
        assert escape_sql("it's") == "it''s"

    def test_multiple_quotes(self):
        assert escape_sql("it's a 'test'") == "it''s a ''test''"

    def test_empty_string(self):
        assert escape_sql("") == ""

    def test_only_quotes(self):
        assert escape_sql("'''") == "''''''"

    def test_no_double_quote_escaping(self):
        assert escape_sql('say "hello"') == 'say "hello"'
