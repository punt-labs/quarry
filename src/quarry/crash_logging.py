"""Report uncaught failures once without serializing exception messages."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import traceback
from typing import TYPE_CHECKING, final

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)


@final
class UncaughtExceptionLog:
    """Install privacy-safe exception hooks in the daemon process."""

    @staticmethod
    def install() -> None:
        """Replace unredacted interpreter defaults and preserve custom hooks."""
        UncaughtExceptionLog._install_system()
        UncaughtExceptionLog._install_thread()

    @classmethod
    def _install_system(cls) -> None:
        prior_system = sys.excepthook

        def system(
            kind: type[BaseException], exc: BaseException, frames: TracebackType | None
        ) -> None:
            if issubclass(kind, KeyboardInterrupt):
                logger.info("interrupted")
            else:
                logger.critical("uncaught exception %s", cls._details(exc))
            if prior_system is not sys.__excepthook__:
                prior_system(kind, exc, frames)

        sys.excepthook = system

    @staticmethod
    def _install_thread() -> None:
        prior_thread = threading.excepthook

        def thread(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is not None:
                logger.critical(
                    "uncaught exception in thread %s %s",
                    getattr(args.thread, "name", "unknown"),
                    UncaughtExceptionLog._details(args.exc_value),
                )
            if prior_thread is not threading.__excepthook__:
                prior_thread(args)

        threading.excepthook = thread

    @classmethod
    def bind_loop(cls, loop: asyncio.AbstractEventLoop) -> None:
        """Log task failures without rendering untrusted event-loop context."""
        prior = loop.get_exception_handler()

        def handler(
            loop: asyncio.AbstractEventLoop, context: dict[str, object]
        ) -> None:
            source = context.get("future") or context.get("task")
            name = source.get_name() if isinstance(source, asyncio.Task) else ""
            exc = context.get("exception")
            detail = cls._details(exc) if isinstance(exc, BaseException) else ""
            logger.error("Unhandled event-loop error (task=%s) %s", name, detail)
            if prior is not None:
                prior(loop, context)

        loop.set_exception_handler(handler)

    @staticmethod
    def _details(exc: BaseException) -> str:
        return (
            f"({type(exc).__name__})\n{''.join(traceback.format_tb(exc.__traceback__))}"
        )
