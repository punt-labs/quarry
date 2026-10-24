"""Tests for quarry.capture_naming: a capture's name, and whether it is a page."""

from __future__ import annotations

import logging

import pytest

from quarry.capture_naming import CaptureNaming

_URL = "https://example.com/page"


class TestName:
    def test_an_explicit_name_is_used_stripped(self) -> None:
        assert CaptureNaming.name("  notes  ", "abcdef123456") == "notes"

    def test_a_session_id_gives_a_prefixed_name(self) -> None:
        assert CaptureNaming.name("", "abcdef1234567890") == "session-abcdef12"

    @pytest.mark.parametrize(("name", "session"), [("", ""), ("  ", "\t")])
    def test_nothing_usable_is_a_value_error(self, name: str, session: str) -> None:
        with pytest.raises(ValueError, match="Missing document_name or session_id"):
            CaptureNaming.name(name, session)


class TestIsPage:
    def test_a_name_that_is_the_sources_page_name_is_a_page(self) -> None:
        assert CaptureNaming.is_page(_URL, _URL)

    def test_a_different_name_is_not(self) -> None:
        assert not CaptureNaming.is_page("https://example.com/other", _URL)

    def test_no_source_is_not_a_page(self) -> None:
        assert not CaptureNaming.is_page(_URL, "")

    def test_an_unstorable_source_is_not_a_page(self) -> None:
        assert not CaptureNaming.is_page("x", "not a url")

    def test_a_mismatch_with_a_source_is_logged_once_with_no_name_or_url(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="quarry.capture_naming"):
            CaptureNaming.is_page("https://example.com/other", _URL)
        [record] = caplog.records
        assert "example.com" not in record.getMessage()

    def test_no_source_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="quarry.capture_naming"):
            CaptureNaming.is_page("session-abc", "")
        assert caplog.records == []
