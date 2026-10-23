"""Tests for quarry.log_redaction: no URL secret reaches a written log line."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import pytest

from quarry.log_redaction import RedactingFileHandler, RedactingStreamHandler
from quarry.log_redactor import LogRedactor

if TYPE_CHECKING:
    from pathlib import Path


def _record(name: str, level: int, msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, args, None)


class TestHandlers:
    def test_the_formatted_line_is_redacted(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = _record("quarry.x", logging.WARNING, "fetch %s", "http://u:S@h/p?t=1")
        assert handler.format(record) == "fetch http://h/…(segments=1)"

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
            == "quarry.test_log_redaction fetching https://h.test/…(segments=1)\n"
        )


class TestHandleError:
    """A failing emit must not echo the record's message or arguments to stderr."""

    def test_the_message_and_arguments_never_reach_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class _Broken(io.StringIO):
            def write(self, _text: str) -> int:
                msg = "disk gone while writing https://u:PWSEC@h.test/p?t=QSEC"
                raise OSError(msg)

        handler = RedactingStreamHandler(_Broken())
        record = _record(
            "quarry.x", logging.WARNING, "fetch %s", "https://u:PWSEC@h.test/p?t=QSEC"
        )
        handler.handle(record)
        err = capsys.readouterr().err
        assert err.startswith("--- Logging error ---\n")
        assert "OSError" in err
        for secret in ("PWSEC", "QSEC", "fetch", "disk gone"):
            assert secret not in err

    def test_nothing_is_written_when_logging_errors_are_silenced(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(logging, "raiseExceptions", False)

        class _Broken(io.StringIO):
            def write(self, _text: str) -> int:
                raise OSError

        RedactingStreamHandler(_Broken()).handle(_record("quarry.x", logging.INFO, "m"))
        assert capsys.readouterr().err == ""


class TestContinuationLines:
    """No line of a traceback or stack may begin a record (D4)."""

    @staticmethod
    def _emit(**kwargs: object) -> str:
        buf = io.StringIO()
        handler = RedactingStreamHandler(buf)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger = logging.getLogger("quarry.continuation")
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            try:
                msg = str(kwargs["exc_message"])
                raise ValueError(msg)
            except ValueError:
                logger.exception("failed")
        finally:
            logger.removeHandler(handler)
        return buf.getvalue()

    def test_a_newline_in_an_exception_message_cannot_forge_a_record(self) -> None:
        out = self._emit(
            exc_message="boom\n2026-01-01 00:00:00 [CRITICAL] quarry.auth: FORGED"
        )
        lines = out.splitlines()
        assert lines[0].endswith("[ERROR] failed")
        assert all(line.startswith("\t") for line in lines[1:])
        assert not [line for line in lines if line.startswith("2026-01-01")]

    def test_a_carriage_return_in_an_exception_message_is_escaped(self) -> None:
        out = self._emit(exc_message="a\rb")
        assert "\r" not in out
        assert "a\\rb" in out

    @pytest.mark.parametrize(
        ("char", "escaped"),
        [
            ("\u202e", "\\u202e"),
            ("\u2066", "\\u2066"),
            ("\u200e", "\\u200e"),
            ("\u200f", "\\u200f"),
            ("\u061c", "\\u061c"),
        ],
    )
    def test_bidi_marks_are_escaped_in_a_traceback_and_a_message(
        self, char: str, escaped: str
    ) -> None:
        assert escaped in self._emit(exc_message=f"a{char}b")
        shown = LogRedactor.one_line(f"a{char}b")
        assert shown == f"a{escaped}b"

    def test_stack_info_continuation_lines_are_indented(self) -> None:
        buf = io.StringIO()
        handler = RedactingStreamHandler(buf)
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            "quarry.x", logging.INFO, __file__, 1, "stack", (), None
        )
        record.stack_info = (
            "Stack (most recent call last):\n  File x\n2026-01-01 FORGED"
        )
        handler.handle(record)
        lines = buf.getvalue().splitlines()
        assert lines[0] == "stack"
        assert all(line.startswith("\t") for line in lines[1:])
