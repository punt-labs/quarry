"""Record a queued job's terminal status when its body exits."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, final

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.failure_reason import FailureReason
from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from collections.abc import Generator

    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
class JobTerminal:
    """Record a queued job's terminal status as its body exits.

    The producer may have withdrawn the job and its store may outlive its worker,
    so the body's exit never overwrites a status somebody else settled
    (:class:`~quarry.daemon.job_lifecycle.JobLifecycle` refuses it): a cancel
    hands the job's store on to its observer, and an exception fails a job the
    worker still owns.
    """

    __slots__ = ()

    @staticmethod
    @contextmanager
    def recording(state: TaskState) -> Generator[None]:
        """Record *state*'s terminal status when its job body exits.

        Cancellation is recorded and re-raised; any other exception is logged
        once, by class and frames, and recorded as ``FailureReason.of`` words it;
        a body that sets no terminal status fails.
        """
        lifecycle = JobLifecycle(state)
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
