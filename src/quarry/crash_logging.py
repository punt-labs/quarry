"""Route uncaught exceptions into the log file instead of only to stderr.

A daemon's stderr goes wherever its supervisor points it — for launchd, a file
with no timestamps, which is how a traceback from one morning can be mistaken
for a live outage that afternoon.  Logging the exception instead puts it in the
same timestamped, rotating file as the operations that led to it, so the
sequence reads in one place.

Three hooks, because an uncaught exception has three ways out of a daemon and
each is a separate interpreter mechanism: the main thread's ``sys.excepthook``,
a worker thread's ``threading.excepthook``, and the event loop's exception
handler for a task nobody awaited.  Missing any one of them leaves a class of
failure invisible in the file.

Every hook delegates to the previously installed one afterwards, so nothing is
taken away by adding ours.  What that preserves differs by hook, and the
difference is worth knowing.  The two interpreter hooks delegate to handlers
that write stderr DIRECTLY, which keeps the supervisor's file as a backstop for
a crash before logging is configured or one that breaks logging itself.  The
loop handler's chain is not that: ``default_exception_handler`` reports through
the ``asyncio`` LOGGER, so its copy travels the same root handlers as ours and
lands in the file too.

The trade there is deliberate: a loop failure is recorded twice, once by us with
the task name and once by asyncio, and two entries for one fault is a smaller
cost than dropping the detail asyncio renders that we do not.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable
    from types import TracebackType

logger = logging.getLogger(__name__)


@final
class UncaughtExceptionLog:
    """Install hooks that log uncaught exceptions before the default handling."""

    __slots__ = ("_prior_system", "_prior_thread")

    # The exact shapes the interpreter installs, so the delegation below needs
    # no callable() narrowing: sys.excepthook takes the exception triple,
    # threading.excepthook takes the single args object.
    _prior_system: Callable[
        [type[BaseException], BaseException, TracebackType | None], None
    ]
    # ``object`` return, not ``None``: typeshed types threading.excepthook's
    # return as object, and narrowing it here would reject the real hook.
    _prior_thread: Callable[[threading.ExceptHookArgs], object]

    def __new__(cls) -> Self:
        """Snapshot the installed system and thread exception hooks for delegation."""
        self = super().__new__(cls)
        self._prior_system = sys.excepthook
        self._prior_thread = threading.excepthook
        return self

    @classmethod
    def install(cls) -> Self:
        """Install the main-thread and worker-thread hooks; return the installer.

        The event loop's handler is installed separately by :meth:`bind_loop`,
        because the loop does not exist yet when the process configures logging.
        """
        self = cls()
        sys.excepthook = self._on_system_exception
        threading.excepthook = self._on_thread_exception
        return self

    @staticmethod
    def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
        """Route the loop's unhandled-exception reports into the log.

        The message alone is not enough: asyncio puts the failing object under
        ``future`` (or ``task``), and its repr is what carries the task NAME.
        In a daemon whose tasks are named, that name is the forensic detail
        this logging exists to preserve, so it goes into the logged line.

        Delegation matches the other two hooks: whatever handler the loop
        already had is captured here and called afterwards, falling back to
        ``default_exception_handler`` when there is none.  Chaining to the
        default unconditionally would have made this the one hook that can
        DESTROY behaviour -- a component that installed its own handler would
        simply stop being called.  Nothing in the daemon's path installs one
        today (uvicorn does not; anyio's is on its TestRunner, which is test
        infrastructure), but "nothing does yet" is not a property worth
        depending on when capturing the predecessor costs one line.

        Note what the chain preserves, which differs from the interpreter
        hooks: ``default_exception_handler`` reports through the ``asyncio``
        LOGGER rather than writing stderr directly, so its copy travels the
        same root handlers as ours and reaches the file too.  A loop failure is
        therefore recorded twice -- the deliberate trade being two entries for
        one fault rather than silently keeping less than the default would.
        """
        prior = loop.get_exception_handler()

        def handler(
            loop: asyncio.AbstractEventLoop, context: dict[str, object]
        ) -> None:
            message = context.get("message", "unhandled exception in event loop")
            source = context.get("future") or context.get("task")
            where = f" in {source!r}" if source is not None else ""
            exception = context.get("exception")
            if isinstance(exception, BaseException):
                logger.error("%s%s", message, where, exc_info=exception)
            else:
                logger.error("%s%s (context: %r)", message, where, context)
            if prior is not None:
                prior(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(handler)

    def _on_system_exception(
        self,
        exc_type: type[BaseException],
        exc: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        """Log a main-thread exception, then let the prior hook run."""
        # A Ctrl-C is a shutdown, not a fault: log it as such and do not dress
        # it up with an ERROR and a traceback.
        if issubclass(exc_type, KeyboardInterrupt):
            logger.info("interrupted")
        else:
            logger.critical("uncaught exception", exc_info=(exc_type, exc, traceback))
        self._prior_system(exc_type, exc, traceback)

    def _on_thread_exception(self, args: threading.ExceptHookArgs) -> None:
        """Log a worker-thread exception, then let the prior hook run."""
        exc = args.exc_value
        name = args.thread.name if args.thread is not None else "unknown"
        if exc is not None:
            logger.critical("uncaught exception in thread %s", name, exc_info=exc)
        self._prior_thread(args)
