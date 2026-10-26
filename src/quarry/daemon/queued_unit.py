"""One admitted job paired with the task state it reports through."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, final

from quarry.redacted_failure import RedactedFailure

if TYPE_CHECKING:
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.tasks import TaskState

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True, slots=True)
class QueuedUnit:
    """A job a collection's worker holds, and the state it reports through."""

    job: IngestUnit
    state: TaskState

    def log_failure(self, what: str, failure: BaseException) -> None:
        """Log that *what* failed for this job, by its kind and collection only."""
        logger.error(
            "abort: %s a %s job for %s failed",
            what,
            type(self.job).__name__,
            self.job.collection,
            exc_info=RedactedFailure.of(failure),
        )
