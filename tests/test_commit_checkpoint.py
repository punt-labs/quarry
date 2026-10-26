"""QueuedJob.checkpoint: a withdrawn job stops before it deletes or stores anything."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.queued_job import JobWithdrawnError, QueuedJob
from quarry.daemon.tasks import TaskState
from quarry.ingestion.chunk_store_funnel import ChunkStoreFunnel
from quarry.ingestion.ingest_context import Progress
from quarry.ingestion.owned_insert import OwnedInsert
from quarry.ingestion.run_context import RunContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from quarry.daemon.queued_ingest import QueuedIngest
    from quarry.daemon.thread_call import ThreadCall
    from quarry.ingestion.extracted_document import ExtractedDocument
    from quarry.ingestion.ingest_context import IngestContext
    from quarry.models import Chunk


def _running() -> tuple[TaskState, Callable[[str], None]]:
    state = TaskState(task_id="j", kind="backfill", status="queued")
    moved = JobLifecycle(state).start()
    assert moved
    queued = cast("QueuedIngest", SimpleNamespace(new_state=lambda: state))
    return state, QueuedJob(cast("ThreadCall", None), queued).checkpoint


def test_other_progress_lines_pass_whatever_became_of_the_job() -> None:
    state, checkpoint = _running()
    JobLifecycle(state).withdraw()

    checkpoint("Chunking")
    checkpoint("Created 3 chunks")


def test_the_commit_point_claims_the_commit() -> None:
    state, checkpoint = _running()

    checkpoint(QueuedJob.COMMIT_POINT)

    assert state.status == "committing"


def test_a_job_withdrawn_before_the_commit_point_stops_there() -> None:
    state, checkpoint = _running()
    JobLifecycle(state).withdraw()

    with pytest.raises(JobWithdrawnError, match="withdrawn before its commit"):
        checkpoint(QueuedJob.COMMIT_POINT)


def test_the_pipeline_reports_the_commit_point_before_it_deletes_anything() -> None:
    events: list[str] = []
    context = cast("IngestContext", SimpleNamespace(url_sourced=False, collection="c"))
    funnel = ChunkStoreFunnel(
        "doc", Progress(lambda message: events.append(f"progress:{message}")), context
    )
    chunks = cast("list[Chunk]", [SimpleNamespace(document_name="doc")])
    extracted = SimpleNamespace(pages=[], stats=SimpleNamespace(as_result_fields=dict))

    def replace_prior(_self: ChunkStoreFunnel, _chunks: list[Chunk]) -> list[str]:
        events.append("delete")
        return []

    with (
        patch.object(ChunkStoreFunnel, "_chunk", lambda _self, _extracted: chunks),
        patch.object(
            ChunkStoreFunnel,
            "_replace_prior",
            replace_prior,
        ),
        patch.object(OwnedInsert, "run", lambda _self, _chunks: 1),
        patch.object(RunContext, "note_stored"),
        patch.object(
            RunContext,
            "legacy_names_of",
            lambda _context: SimpleNamespace(drop_legacy=lambda *_args: None),
        ),
    ):
        funnel.run(cast("ExtractedDocument", extracted))

    commit = f"progress:{QueuedJob.COMMIT_POINT}"
    assert events.index(commit) < events.index("delete")
