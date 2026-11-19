"""Check safe diagnostics for uncaught main, thread and event-loop failures."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from quarry.crash_logging import UncaughtExceptionLog

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture()
def _restore_hooks() -> Generator[None]:
    """Put the interpreter's hooks back, whatever the test did to them."""
    prior_system, prior_thread = sys.excepthook, threading.excepthook
    yield
    sys.excepthook, threading.excepthook = prior_system, prior_thread


@pytest.mark.usefixtures("_restore_hooks")
class TestMainThread:
    def test_the_exception_is_logged_with_its_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        UncaughtExceptionLog.install()
        # The exception object rather than sys.exc_info(): the latter is typed
        # as an optional triple, so splatting it into excepthook needs a cast or
        # a suppression to satisfy a strict checker.  This is precise as written.
        try:
            msg = "boom"
            raise RuntimeError(msg)
        except RuntimeError as exc:
            with caplog.at_level(logging.CRITICAL):
                sys.excepthook(type(exc), exc, exc.__traceback__)
        assert "uncaught exception" in caplog.text
        assert "raise RuntimeError(msg)" in caplog.text
        assert "RuntimeError)" in caplog.text
        assert "RuntimeError: boom" not in caplog.text

    def test_the_prior_hook_still_runs(self) -> None:
        """A non-default system hook still receives the original exception."""
        seen: list[str] = []
        sys.excepthook = lambda *args: seen.append(args[0].__name__)
        UncaughtExceptionLog.install()
        try:
            msg = "boom"
            raise ValueError(msg)
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        assert seen == ["ValueError"]

    def test_keyboard_interrupt_is_a_shutdown_not_a_fault(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Ctrl-C must not be recorded as a crash with a traceback."""
        UncaughtExceptionLog.install()
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt as exc:
            with caplog.at_level(logging.INFO):
                sys.excepthook(type(exc), exc, exc.__traceback__)
        assert "interrupted" in caplog.text
        assert "uncaught exception" not in caplog.text


@pytest.mark.usefixtures("_restore_hooks")
class TestWorkerThread:
    def test_a_thread_exception_is_logged_with_the_thread_name(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A worker dying silently is the case sys.excepthook does not cover."""
        UncaughtExceptionLog.install()

        def boom() -> None:
            msg = "worker died"
            raise RuntimeError(msg)

        with caplog.at_level(logging.CRITICAL):
            worker = threading.Thread(target=boom, name="ingest-worker")
            worker.start()
            worker.join()
        assert "uncaught exception in thread ingest-worker" in caplog.text
        assert "RuntimeError)" in caplog.text
        assert "RuntimeError: worker died" not in caplog.text


class TestEventLoop:
    def test_an_unawaited_task_failure_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The third escape route: a task nobody awaited."""

        async def main() -> None:
            UncaughtExceptionLog.bind_loop(asyncio.get_running_loop())

            async def boom() -> None:
                msg = "task died"
                raise RuntimeError(msg)

            task = asyncio.create_task(boom())
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError):
                await task
            # Report a failure the way the loop itself would, since a task that
            # IS awaited never reaches the handler.
            asyncio.get_running_loop().call_exception_handler(
                {
                    "message": "Task exception was never retrieved",
                    "exception": task.exception(),
                }
            )

        with caplog.at_level(logging.ERROR):
            asyncio.run(main())
        assert "Unhandled event-loop error" in caplog.text
        assert "RuntimeError)" in caplog.text
        assert "RuntimeError: task died" not in caplog.text

    def test_the_task_name_survives_into_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Record the task name without using the task representation."""

        async def main() -> None:
            loop = asyncio.get_running_loop()
            UncaughtExceptionLog.bind_loop(loop)

            async def boom() -> None:
                msg = "task died"
                raise RuntimeError(msg)

            task = asyncio.create_task(boom(), name="watch-sweep")
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError):
                await task
            loop.call_exception_handler(
                {
                    "message": "Task exception was never retrieved",
                    "exception": task.exception(),
                    "future": task,
                }
            )

        with caplog.at_level(logging.ERROR):
            asyncio.run(main())
        ours = [r for r in caplog.records if r.name == "quarry.crash_logging"]
        assert ours, "this module must log the failure itself, not only delegate"
        assert "watch-sweep" in ours[0].getMessage(), (
            "the task name must reach OUR record, not just asyncio's copy"
        )

    def test_a_prior_handler_is_called_not_clobbered(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Binding must ADD to whatever the loop already had, never replace it.

        This is the hook that could destroy behaviour rather than merely fail
        to add: a component's own handler would simply stop being called, and
        nothing would report that it had.
        """
        seen: list[str] = []

        async def main() -> None:
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(
                lambda _loop, context: seen.append(str(context.get("message")))
            )
            UncaughtExceptionLog.bind_loop(loop)
            loop.call_exception_handler({"message": "socket leaked"})

        with caplog.at_level(logging.ERROR):
            asyncio.run(main())
        assert seen == ["socket leaked"], "the prior handler must still run"
        ours = [r for r in caplog.records if r.name == "quarry.crash_logging"]
        assert ours, "and ours must run too — delegation is an addition"

    def test_a_context_without_an_exception_still_reports(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Some loop reports carry no exception object; they must not vanish."""

        async def main() -> None:
            loop = asyncio.get_running_loop()
            UncaughtExceptionLog.bind_loop(loop)
            loop.call_exception_handler({"message": "socket leaked"})

        with caplog.at_level(logging.ERROR):
            asyncio.run(main())
        assert "Unhandled event-loop error" in caplog.text
        assert "socket leaked" not in caplog.text
