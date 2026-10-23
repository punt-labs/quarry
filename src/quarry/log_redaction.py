"""Log handlers that redact URLs and drop third-party chatter before it is written."""

from __future__ import annotations

import copy
import logging
import logging.handlers
import sys
import traceback
from contextlib import suppress
from typing import TextIO, final, override

from quarry.log_redactor import LogRedactor


class _FirstPartyHandler(logging.Handler):
    """A handler that redacts its output and keeps only quarry's own chatter.

    A third-party logger (the sitemap parser, urllib3, uvicorn) writes URLs and
    every address it touches at INFO.  Below WARNING only ``quarry*`` records are
    written, so the cap on libraries is the default, not a list of their names.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Keep a quarry record, or any record at WARNING or above."""
        return bool(super().filter(record)) and (
            record.name.startswith("quarry") or record.levelno >= logging.WARNING
        )

    def format(self, record: logging.LogRecord) -> str:
        """Return the formatted record, traceback included, redacted.

        The message is made one line first, on a copy of the record (the other
        handler formats the same one), so a forged line cannot start inside it;
        the traceback and stack keep their own newlines but every line is indented,
        so none can begin a record.
        """
        single = copy.copy(record)
        single.msg, single.args = LogRedactor.one_line(record.getMessage()), ()
        single.exc_text = self._traceback(record)
        single.exc_info = None
        if record.stack_info:
            single.stack_info = LogRedactor.continuation(record.stack_info)
        settled = LogRedactor.settle(super().format(single))
        return self._withheld(record) if settled is None else settled

    def _withheld(self, record: logging.LogRecord) -> str:
        """Return the one fixed line that stands for a record that did not settle.

        Built from what the code itself writes: the time (in the handler's own
        format), level, logger and call site, then the message template if it
        settles on its own.  No arguments, no traceback text, and no log call.
        The line goes through the redactor too, so a secret-shaped path or logger
        name is masked; if even that does not settle, a line with no fields.
        """
        formatter = self.formatter or logging.Formatter()
        when = formatter.formatTime(record, formatter.datefmt)
        where = f"{record.name} {record.pathname}:{record.lineno}"
        line = (
            f"{when} {record.levelname} {where}: message withheld: "
            f"redaction did not settle in {LogRedactor.PASSES} passes"
        )
        template = self._template(record)
        if template is not None:
            line = f"{line} [{template}]"
        return self._fixed(line, "message withheld: redaction did not settle")

    @staticmethod
    def _template(record: logging.LogRecord) -> str | None:
        """Return the record's message template if it settles on its own."""
        if not isinstance(record.msg, str):
            return None
        return LogRedactor.settle(LogRedactor.one_line(record.msg))

    @staticmethod
    def _fixed(line: str, fallback: str) -> str:
        """Return *line* one-line and redacted, or *fallback* if it will not settle."""
        settled = LogRedactor.settle(LogRedactor.one_line(line))
        return fallback if settled is None else settled

    def _traceback(self, record: logging.LogRecord) -> str | None:
        """Return the record's traceback with every line indented, or ``None``."""
        if record.exc_info:
            formatter = self.formatter or logging.Formatter()
            return LogRedactor.continuation(formatter.formatException(record.exc_info))
        return LogRedactor.continuation(record.exc_text) if record.exc_text else None

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        """Report an emit failure without echoing the record.

        The stdlib prints the raw message and arguments, which is where a URL
        secret lives, straight to stderr.  This writes a fixed line, the logger's
        name, the exception's class, where the call was made, its message template
        (code-authored, but redacted all the same) and message-free frames.  The
        arguments stay out.
        """
        error = sys.exception()
        if not logging.raiseExceptions or error is None or sys.stderr is None:
            return
        frames = "".join(traceback.format_tb(error.__traceback__))
        template = (
            record.msg if isinstance(record.msg, str) else type(record.msg).__name__
        )
        who = f"{record.name}: {type(error).__name__}"
        where = f"at {record.pathname}:{record.lineno}: {template}"
        # Code-authored, but a path or template may hold a line break or a bidi
        # mark; the frames keep their own newlines.
        report = (
            f"--- Logging error ---\n{LogRedactor.one_line(who)}\n"
            f"{LogRedactor.one_line(where)}\n{frames}"
        )
        settled = LogRedactor.settle(report)
        if settled is None:
            settled = self._fixed(
                f"--- Logging error --- {record.name} at "
                f"{record.pathname}:{record.lineno}: report withheld: "
                f"redaction did not settle in {LogRedactor.PASSES} passes",
                "--- Logging error --- report withheld: redaction did not settle",
            )
            settled += "\n"
        with suppress(OSError, ValueError):  # a dead stderr must not break the caller
            sys.stderr.write(settled)


@final
class RedactingFileHandler(_FirstPartyHandler, logging.handlers.RotatingFileHandler):
    """The rotating log file, redacted and first-party below WARNING."""


@final
class RedactingStreamHandler(_FirstPartyHandler, logging.StreamHandler[TextIO]):
    """The stderr stream, redacted and first-party below WARNING."""
