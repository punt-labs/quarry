"""Tests for quarry.log_redaction: no URL secret reaches a written log line."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from quarry.log_redaction import RedactingFileHandler, RedactingStreamHandler

if TYPE_CHECKING:
    from pathlib import Path


def _record(name: str, level: int, msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


class TestHandlers:
    def test_the_formatted_line_is_redacted(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = _record("quarry.x", logging.WARNING, "fetch %s", "http://u:S@h/p?t=1")
        assert handler.format(record) == "fetch http://h/…(1 segment)"

    def test_the_traceback_text_is_redacted_too(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        try:
            msg = "boom http://h/p?token=SECRET"
            raise RuntimeError(msg)
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                "quarry.x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        text = handler.format(record)
        assert "RuntimeError" in text
        assert "SECRET" not in text

    def test_a_quarry_record_below_warning_is_kept(self) -> None:
        assert RedactingStreamHandler().filter(_record("quarry.x", logging.INFO, "m"))

    @pytest.mark.parametrize("name", ["usp.helpers", "urllib3", "httpx", "uvicorn"])
    def test_a_third_party_record_below_warning_is_dropped(self, name: str) -> None:
        assert not RedactingStreamHandler().filter(_record(name, logging.INFO, "m"))

    def test_a_third_party_warning_is_kept_for_the_formatter_to_redact(self) -> None:
        assert RedactingStreamHandler().filter(
            _record("usp.helpers", logging.WARNING, "m")
        )

    def test_the_file_handler_redacts_what_it_writes(self, tmp_path: Path) -> None:
        log = tmp_path / "x.log"
        handler = RedactingFileHandler(str(log), encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
        logger = logging.getLogger("quarry.test_log_redaction")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            logger.info("fetching %s", "https://u:SECRET@h.test/p?token=SECRET")
            logging.getLogger("usp.helpers").addHandler(handler)
            logging.getLogger("usp.helpers").setLevel(logging.INFO)
            logging.getLogger("usp.helpers").info("noise https://h/x?token=SECRET")
        finally:
            logging.getLogger("usp.helpers").removeHandler(handler)
            logging.getLogger("usp.helpers").setLevel(logging.NOTSET)
            logger.removeHandler(handler)
            handler.close()
        assert (
            log.read_text()
            == "quarry.test_log_redaction fetching https://h.test/…(1 segment)\n"
        )
