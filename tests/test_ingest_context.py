"""Tests for quarry.ingestion.ingest_context — Progress and IngestContext."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from quarry.ingestion.ingest_context import IngestContext, Progress


class TestProgress:
    def test_calls_logger_and_callback(self, caplog: pytest.LogCaptureFixture) -> None:
        messages: list[str] = []
        progress = Progress(messages.append)

        with caplog.at_level(logging.INFO, logger="quarry.ingestion.ingest_context"):
            progress("Processed %d items", 3)

        assert messages == ["Processed 3 items"]
        assert "Processed 3 items" in caplog.text

    def test_no_callback_still_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        progress = Progress(None)

        with caplog.at_level(logging.INFO, logger="quarry.ingestion.ingest_context"):
            progress("hello")

        assert "hello" in caplog.text

    def test_no_args_formats_bare_message(self) -> None:
        messages: list[str] = []
        progress = Progress(messages.append)

        progress("no substitutions here")

        assert messages == ["no substitutions here"]

    def test_silent_calls_neither_logger_nor_callback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: plan_file_chunks must not gain new log lines (design §b)."""
        messages: list[str] = []
        progress = Progress.silent()

        with caplog.at_level(logging.INFO, logger="quarry.ingestion.ingest_context"):
            progress("should not appear", 1, 2)

        assert messages == []
        assert caplog.text == ""


class TestIngestContext:
    def test_defaults(self) -> None:
        context = IngestContext(database=MagicMock(), settings=MagicMock())

        assert context.overwrite is False
        assert context.collection == "default"
        assert context.agent_handle == ""
        assert context.memory_type == ""
        assert context.summary == ""

    def test_is_frozen(self) -> None:
        context = IngestContext(database=MagicMock(), settings=MagicMock())

        with pytest.raises(AttributeError):
            context.overwrite = True  # type: ignore[misc]
