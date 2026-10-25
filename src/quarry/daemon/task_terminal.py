"""Record a background task's terminal status when its body exits."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, final

from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Generator

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class TaskTerminal:
    """Record a background task's terminal status as its body exits."""

    __slots__ = ()

    @staticmethod
    @contextmanager
    def recording(state: TaskState) -> Generator[None]:
        """Record *state*'s terminal status when its background body exits.

        Cancellation is recorded and re-raised, keeping a reason the run already
        gave; any other exception is logged once, by class and frames, and
        recorded as ``FailureReason.of`` words it: a refusal's text, else
        ``internal error: <Class>`` (a ReportedError's carried class); a body
        that sets no terminal status fails.
        """
        try:
            yield
        except asyncio.CancelledError:
            state.status = "failed"
            state.error = state.reason_or("task was cancelled")
            raise
        except Exception as exc:
            logger.error(
                "Background %s failed", state.kind, exc_info=RedactedFailure.of(exc)
            )
            state.status = "failed"
            state.error = FailureReason.of(exc)
        finally:
            if state.status == "running":
                state.status = "failed"
                state.error = "task exited without setting terminal status"
