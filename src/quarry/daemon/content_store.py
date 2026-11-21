"""Run an inline content job and record its terminal outcome."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from quarry.daemon.refusal_aware_terminal import RefusalAwareTerminal
from quarry.daemon.store_run import StoreRun

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.task_state import TaskState


@final
class ContentStore:
    """Coordinate the store thread and terminal state for inline content jobs."""

    @staticmethod
    async def run(
        job: IngestUnit,
        state: TaskState,
        store_content: Callable[[], dict[str, object]],
        diagnostic_name: Callable[[], str],
    ) -> None:
        """Run the store and record its outcome with a diagnostic-safe subject."""
        terminal = RefusalAwareTerminal.for_job(state, diagnostic_name)
        with terminal.record():
            store = StoreRun.start(
                job, state, store_content, RefusalAwareTerminal.settlement
            )
            terminal.apply(await store.wait())

    @staticmethod
    def is_stored(result: Mapping[str, object]) -> bool:
        """Return whether the outcome leaves content in the store."""
        return RefusalAwareTerminal.is_stored(result)
