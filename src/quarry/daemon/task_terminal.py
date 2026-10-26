"""Record a background task's terminal status when its body exits."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, final

from quarry.daemon.task_lifecycle import TaskLifecycle
from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Generator

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class TaskTerminal:
    """Record a polled background task's terminal status as its body exits.

    For a task that is not a queued job (a sync, a delete, a registration, a
    backfill); a queued job's body ends through
    :class:`~quarry.daemon.job_terminal.JobTerminal`.
    """

    __slots__ = ()

    @staticmethod
    @contextmanager
    def recording(state: TaskState) -> Generator[None]:
        """Record *state*'s terminal status when its background body exits.

        Cancellation is recorded and re-raised, failing the task whatever it had
        recorded and keeping a reason the run already gave; any other exception
        is logged once, by class and frames, and recorded as ``FailureReason.of``
        words it: a refusal's text, else ``internal error: <Class>`` (a
        ReportedError's carried class); a body that sets no terminal status fails.
        """
        lifecycle = TaskLifecycle(state)
        try:
            yield
        except asyncio.CancelledError:
            lifecycle.cancelled("task was cancelled")
            raise
        except Exception as exc:
            logger.error(
                "Background %s failed", state.kind, exc_info=RedactedFailure.of(exc)
            )
            lifecycle.settle_failed(FailureReason.of(exc))
        finally:
            lifecycle.exited("task exited without setting terminal status")
