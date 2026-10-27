"""Tests for quarry.log_redaction: no URL secret reaches a written log line."""

from __future__ import annotations

import io
import logging
import sys
from typing import TYPE_CHECKING, cast

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
        for secret in ("PWSEC", "QSEC", "disk gone"):
            assert secret not in err

    def test_a_broken_format_call_shows_where_and_the_template_not_the_arguments(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        handler = RedactingStreamHandler(io.StringIO())
        record = logging.LogRecord(
            "quarry.x",
            logging.WARNING,
            "/src/quarry/caller.py",
            321,
            "count %d for %s",
            ("not-an-int", "https://u:PWSEC@h.test/p?t=QSEC"),
            None,
        )
        handler.handle(record)
        err = capsys.readouterr().err
        assert "/src/quarry/caller.py:321" in err
        assert "count %d for %s" in err
        for secret in ("not-an-int", "PWSEC", "QSEC"):
            assert secret not in err

    @pytest.mark.parametrize(
        "template",
        ["a\nb", "a\rb", "a\u202eb", "a\u200fb", "a\n2026-01-01 [CRITICAL] FORGED"],
        ids=["lf", "cr", "rlo", "rlm", "forged"],
    )
    def test_the_template_and_location_are_one_line(
        self, template: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record = logging.LogRecord(
            "quarry.x",
            logging.WARNING,
            "/src/a\nb.py",
            7,
            template + " %d",
            ("x",),
            None,
        )
        RedactingStreamHandler(io.StringIO()).handle(record)
        err = capsys.readouterr().err
        lines = err.splitlines()
        assert lines[0] == "--- Logging error ---"
        assert lines[1] == "quarry.x: TypeError"
        assert lines[2].startswith("at /src/a")
        assert not [line for line in lines if line.startswith("2026-01-01")]
        assert "\r" not in err
        assert "\u202e" not in err
        assert "\u200f" not in err
        assert "a\\nb.py:7" in err

    @pytest.mark.parametrize("failure", [OSError("closed"), ValueError("closed file")])
    def test_a_broken_stderr_cannot_raise_into_the_caller(
        self, monkeypatch: pytest.MonkeyPatch, failure: Exception
    ) -> None:
        class _Broken(io.StringIO):
            def write(self, _text: str) -> int:
                raise OSError

        class _DeadStderr:
            def write(self, _text: str) -> int:
                raise failure

        monkeypatch.setattr("sys.stderr", _DeadStderr())
        RedactingStreamHandler(_Broken()).handle(
            _record("quarry.x", logging.WARNING, "m")
        )

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


_POISON = "POISON"


@pytest.fixture
def poisoned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any text holding ``POISON`` fail to settle; all other text redacts."""
    real = LogRedactor._once  # pyright: ignore[reportPrivateUsage]

    def once(text: str) -> str:
        return text + "x" if _POISON in text else real(text)

    monkeypatch.setattr(LogRedactor, "_once", once)


@pytest.mark.usefixtures("poisoned")
class TestARecordThatDoesNotSettle:
    @staticmethod
    def _handler() -> RedactingStreamHandler:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    def test_one_fixed_line_stands_for_it(self) -> None:
        record = _record("quarry.x", logging.ERROR, "fetch %s", _POISON)
        line = self._handler().format(record)
        assert "message withheld: redaction did not settle in 8 passes" in line
        assert "\n" not in line

    def test_the_line_names_the_level_the_logger_and_the_call_site(self) -> None:
        line = self._handler().format(_record("quarry.x", logging.ERROR, "%s", _POISON))
        assert all(part in line for part in ("ERROR", "quarry.x", f"{__file__}:1"))

    def test_the_line_carries_no_arguments_or_traceback_text(self) -> None:
        try:
            msg = f"boom SECRET2 {_POISON}"
            raise RuntimeError(msg)
        except RuntimeError:
            record = logging.LogRecord(
                "quarry.x",
                logging.ERROR,
                __file__,
                1,
                "SECRET1 %s",
                (f"SECRET3 {_POISON}",),
                sys.exc_info(),
            )
        line = self._handler().format(record)
        assert not any(s in line for s in ("SECRET2", "SECRET3", _POISON))

    def test_a_template_that_settles_is_kept_in_the_line(self) -> None:
        line = self._handler().format(
            _record("quarry.x", logging.ERROR, "fetch %s failed", _POISON)
        )
        assert "[fetch %s failed]" in line

    def test_a_template_that_does_not_settle_is_left_out(self) -> None:
        line = self._handler().format(
            _record("quarry.x", logging.ERROR, f"{_POISON} %s", "a")
        )
        assert _POISON not in line
        assert "message withheld" in line

    def test_the_time_follows_the_handlers_own_format(self) -> None:
        handler = RedactingStreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s", datefmt="DATE-HERE"))
        line = handler.format(_record("quarry.x", logging.ERROR, "%s", _POISON))
        assert line.startswith("DATE-HERE ERROR ")

    def test_a_secret_shaped_call_site_is_masked(self) -> None:
        record = logging.LogRecord(
            "quarry.x",
            logging.ERROR,
            "https://u:PWSEC@h.test/p?q=QSEC",
            1,
            "%s",
            (_POISON,),
            None,
        )
        line = self._handler().format(record)
        assert not any(s in line for s in ("PWSEC", "QSEC"))
        assert "message withheld" in line

    def test_a_line_that_will_not_settle_either_is_a_line_with_no_fields(self) -> None:
        record = logging.LogRecord(
            "quarry.x", logging.ERROR, f"/{_POISON}.py", 1, "%s", (_POISON,), None
        )
        line = self._handler().format(record)
        assert line == "message withheld: redaction did not settle"

    def test_formatting_it_makes_no_log_record(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            self._handler().format(_record("quarry.x", logging.ERROR, "%s", _POISON))
        assert caplog.records == []

    def test_a_file_handler_writes_the_line_once_though_rollover_formats_it(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "x.log"
        handler = RedactingFileHandler(str(log), maxBytes=10_000, encoding="utf-8")
        handler.emit(_record("quarry.x", logging.ERROR, "%s", _POISON))
        handler.close()
        assert log.read_text(encoding="utf-8").count("message withheld") == 1


@pytest.mark.usefixtures("poisoned")
class TestAReportThatDoesNotSettle:
    @staticmethod
    def _broken(pathname: str = "/src/quarry/caller.py") -> logging.LogRecord:
        # %d with a string raises inside format, so handleError runs.
        return logging.LogRecord(
            "quarry.x", logging.WARNING, pathname, 321, f"n=%d {_POISON}", ("a",), None
        )

    def test_one_fixed_line_with_a_newline_is_written(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        RedactingStreamHandler(io.StringIO()).handle(self._broken())
        err = capsys.readouterr().err
        assert err == (
            "--- Logging error --- quarry.x at /src/quarry/caller.py:321: "
            "report withheld: redaction did not settle in 8 passes (TypeError)\n"
        )

    def test_a_call_site_that_will_not_settle_gives_a_line_with_no_fields(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        RedactingStreamHandler(io.StringIO()).handle(self._broken(f"/{_POISON}.py"))
        assert capsys.readouterr().err == (
            "--- Logging error --- report withheld: redaction did not settle\n"
        )


@pytest.mark.usefixtures("poisoned")
class TestTheWithheldLinesNameTheException:
    @staticmethod
    def _typed_error_record() -> logging.LogRecord:
        try:
            msg = "boom"
            raise TypeError(msg)
        except TypeError:
            return logging.LogRecord(
                "quarry.x",
                logging.ERROR,
                __file__,
                1,
                f"%s {_POISON}",
                ("a",),
                sys.exc_info(),
            )

    def test_a_record_with_an_exception_names_its_class(self) -> None:
        handler = RedactingStreamHandler()
        line = handler.format(self._typed_error_record())
        assert "TypeError" in line
        assert "boom" not in line

    def test_a_record_without_one_names_none(self) -> None:
        line = RedactingStreamHandler().format(
            _record("quarry.x", logging.ERROR, "%s", _POISON)
        )
        assert "Error" not in line

    def test_the_report_names_the_class(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record = logging.LogRecord(
            "quarry.x", logging.WARNING, "/c.py", 3, f"n=%d {_POISON}", ("a",), None
        )
        RedactingStreamHandler(io.StringIO()).handle(record)
        err = capsys.readouterr().err
        assert "TypeError" in err
        assert "report withheld" in err

    def test_the_report_keeps_a_template_that_settles(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        real = LogRedactor._once  # pyright: ignore[reportPrivateUsage]

        def once(text: str) -> str:
            # Only the multi-line report fails; the one-line fixed text settles.
            return (
                text + "x" if text.startswith("--- Logging error ---\n") else real(text)
            )

        monkeypatch.setattr(LogRedactor, "_once", once)
        record = logging.LogRecord(
            "quarry.x", logging.WARNING, "/c.py", 3, "n=%d clean", ("a",), None
        )
        RedactingStreamHandler(io.StringIO()).handle(record)
        err = capsys.readouterr().err
        assert "TypeError" in err
        assert "[n=%d clean]" in err


@pytest.mark.usefixtures("poisoned")
class TestTheReportNamesBothClasses:
    def test_a_record_that_carried_an_exception_is_named_with_the_failure(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        try:
            msg = "inner"
            raise ValueError(msg)
        except ValueError:
            record = logging.LogRecord(
                "quarry.x",
                logging.WARNING,
                "/c.py",
                3,
                f"n=%d {_POISON}",
                ("a",),
                sys.exc_info(),
            )
        RedactingStreamHandler(io.StringIO()).handle(record)
        assert "TypeError while logging ValueError" in capsys.readouterr().err


class TestTheSettledReportNamesBothClasses:
    def test_a_report_that_settles_names_the_logged_class_too(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        try:
            msg = "inner"
            raise ValueError(msg)
        except ValueError:
            record = logging.LogRecord(
                "quarry.x", logging.WARNING, "/c.py", 3, "n=%d", ("a",), sys.exc_info()
            )
        RedactingStreamHandler(io.StringIO()).handle(record)
        assert "TypeError while logging ValueError" in capsys.readouterr().err

    def test_a_record_with_a_true_exc_info_does_not_break_the_report(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        flag = True
        record = logging.LogRecord(
            "quarry.x",
            logging.WARNING,
            "/c.py",
            3,
            "n=%d",
            ("a",),
            cast("tuple[None, None, None]", flag),
        )
        RedactingStreamHandler(io.StringIO()).handle(record)
        assert "TypeError" in capsys.readouterr().err


class TestAMalformedExcInfoDoesNotBreakTheReport:
    @staticmethod
    def _report(exc_info: object, capsys: pytest.CaptureFixture[str]) -> str:
        record = logging.LogRecord(
            "quarry.x",
            logging.WARNING,
            "/c.py",
            3,
            "n=%d",
            ("a",),
            cast("tuple[None, None, None]", exc_info),
        )
        RedactingStreamHandler(io.StringIO()).handle(record)
        return capsys.readouterr().err

    def test_an_empty_tuple(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert "TypeError" in self._report((), capsys)

    def test_a_tuple_of_non_exception_classes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert "TypeError" in self._report(("a", "b", "c"), capsys)
