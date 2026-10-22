"""Log handlers that redact URLs and drop third-party chatter before it is written."""

from __future__ import annotations

import copy
import logging
import logging.handlers
import sys
import traceback
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
        the traceback the formatter appends keeps its own newlines.
        """
        single = copy.copy(record)
        single.msg, single.args = LogRedactor.one_line(record.getMessage()), ()
        return LogRedactor.redact(super().format(single))

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        """Report an emit failure without echoing the record.

        The stdlib prints the raw message and arguments, which is where a URL
        secret lives, straight to stderr.  This writes a fixed line, the logger's
        name, the exception's class and a redacted traceback with no message.
        """
        error = sys.exception()
        if not logging.raiseExceptions or error is None or sys.stderr is None:
            return
        frames = "".join(traceback.format_tb(error.__traceback__))
        report = (
            f"--- Logging error ---\n{record.name}: {type(error).__name__}\n{frames}"
        )
        sys.stderr.write(LogRedactor.redact(report))


@final
class RedactingFileHandler(_FirstPartyHandler, logging.handlers.RotatingFileHandler):
    """The rotating log file, redacted and first-party below WARNING."""


@final
class RedactingStreamHandler(_FirstPartyHandler, logging.StreamHandler[TextIO]):
    """The stderr stream, redacted and first-party below WARNING."""
