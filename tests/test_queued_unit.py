"""QueuedUnit: a job and its state, and how the abort names it in a log line."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.daemon.queued_unit import QueuedUnit
from quarry.daemon.tasks import TaskState

if TYPE_CHECKING:
    import pytest

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.job_spool import SpoolRecord


@final
@dataclass(frozen=True, slots=True)
class _Unit:
    collection: str = "notes"

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        del ctx, state

    def spool_record(self) -> SpoolRecord | None:
        return None


def test_a_failure_is_logged_by_the_jobs_kind_and_collection_never_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unit = QueuedUnit(_Unit(), TaskState(task_id="t", kind="remember"))

    with caplog.at_level(logging.ERROR, logger="quarry.daemon"):
        unit.log_failure("spooling", RuntimeError("secret detail"))

    assert "abort: spooling a _Unit job for notes (task t) failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "secret detail" not in caplog.text
