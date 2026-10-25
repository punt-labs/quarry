"""Every content job x every way its body can end -> one terminal state and reason.

One policy owns the terminal write (``RefusalAwareTerminal``), so this table is the
contract: rows stored completes; nothing stored, a refusal and an unexpected error
all fail, each with a reason a client may see and no more.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import MagicMock, patch

import pytest

from quarry.content_guard import UnindexableContentError
from quarry.daemon.captured_body_ingest import IngestLabels
from quarry.daemon.content_jobs import CaptureIngestJob, ScrubbedIngestJob
from quarry.daemon.ingest_jobs import IngestJob
from quarry.daemon.tasks import TaskState
from quarry.http_url import HttpUrl

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext

_NOTHING = {
    "single": "nothing was stored: the content produced no indexable text",
    "crawl": "nothing was stored: no pages were selected for ingest",
}
_LABELS = IngestLabels(
    overwrite=True, collection="c", agent_handle="", memory_type="", summary=""
)
_URL = HttpUrl("https://x.test/page")


class _Job(Protocol):
    async def run(self, ctx: DaemonContext, state: TaskState) -> None: ...


def _inline() -> ScrubbedIngestJob:
    return ScrubbedIngestJob(
        name="note",
        content="body",
        labels=_LABELS,
        format_hint="auto",
        scrub_label="remember",
    )


# name -> (job factory, the method the body runs, a page-count result shape)
_JOBS: dict[str, tuple[Callable[[], _Job], str, str]] = {
    "remember": (_inline, "scrub_and_ingest", "single"),
    "capture-with-source": (
        lambda: CaptureIngestJob(inline=_inline(), source_url=_URL),
        "_capture",
        "single",
    ),
    "capture-without-source": (
        lambda: CaptureIngestJob(inline=_inline(), source_url=None),
        "_capture",
        "single",
    ),
    "plain-ingest": (
        lambda: IngestJob(source=_URL, labels=_LABELS, scrub=False),
        "_ingest",
        "single",
    ),
    "scrubbed-ingest": (
        lambda: IngestJob(source=_URL, labels=_LABELS, scrub=True),
        "_ingest",
        "single",
    ),
    "bulk-crawl": (
        lambda: IngestJob(source=_URL, labels=_LABELS, scrub=False),
        "_ingest",
        "crawl",
    ),
}

_STORED = {"single": {"chunks": 3}, "crawl": {"ingested": 2, "skipped": 0}}
_EMPTY = {"single": {"chunks": 0}, "crawl": {"ingested": 0, "skipped": 0}}
_ALL_SKIPPED = {
    "single": {"chunks": 0, "skipped": "exists"},
    "crawl": {"ingested": 0, "skipped": 4},
}


def _run(job: _Job, method: str, outcome: object) -> TaskState:
    state = TaskState(task_id="t", kind="ingest")
    ctx = cast("DaemonContext", SimpleNamespace())
    body = MagicMock(
        side_effect=outcome if isinstance(outcome, Exception) else None,
        return_value=outcome,
    )
    with patch.object(type(job), method, body):
        asyncio.run(job.run(ctx, state))
    return state


@pytest.mark.parametrize("name", list(_JOBS))
class TestJobOutcomes:
    def test_rows_stored_completes(self, name: str) -> None:
        make, method, shape = _JOBS[name]
        state = _run(make(), method, _STORED[shape])
        assert (state.status, state.error) == ("completed", "")

    def test_everything_already_stored_completes(self, name: str) -> None:
        make, method, shape = _JOBS[name]
        state = _run(make(), method, _ALL_SKIPPED[shape])
        assert (state.status, state.error) == ("completed", "")

    def test_nothing_stored_is_a_refusal(self, name: str) -> None:
        make, method, shape = _JOBS[name]
        state = _run(make(), method, _EMPTY[shape])
        assert (state.status, state.error) == ("failed", _NOTHING[shape])

    def test_a_refusal_keeps_its_reason(self, name: str) -> None:
        make, method, _ = _JOBS[name]
        refusal = UnindexableContentError("binary content rejected")
        state = _run(make(), method, refusal)
        assert (state.status, state.error) == ("failed", "binary content rejected")

    def test_an_unexpected_error_names_only_its_class(self, name: str) -> None:
        make, method, _ = _JOBS[name]
        state = _run(make(), method, OSError("/home/me/secret/key.pem"))
        assert (state.status, state.error) == ("failed", "internal error: OSError")


_CRAWL_ALL_EMPTY = {
    "total_discovered": 3,
    "after_filter": 3,
    "ingested": 0,
    "empty": 3,
    "skipped": 0,
    "failed": 0,
    "errors": [],
}
_CRAWL_NOTHING = {
    "empty sitemap": {"total_discovered": 0, "after_filter": 0, "ingested": 0},
    "filters match nothing": {"total_discovered": 9, "after_filter": 0, "ingested": 0},
    "every entry failed": {"total_discovered": 4, "after_filter": 4, "ingested": 0},
}


@pytest.mark.parametrize("label", list(_CRAWL_NOTHING))
def test_a_crawl_that_selected_no_pages_names_that_reason(label: str) -> None:
    make, method, _ = _JOBS["bulk-crawl"]
    results = {**_CRAWL_NOTHING[label], "skipped": 0, "failed": 0, "errors": []}
    state = _run(make(), method, results)
    assert (state.status, state.error) == ("failed", _NOTHING["crawl"])


def test_a_crawl_whose_pages_were_all_empty_says_the_content_had_no_text() -> None:
    make, method, _ = _JOBS["bulk-crawl"]
    state = _run(make(), method, _CRAWL_ALL_EMPTY)
    assert (state.status, state.error) == ("failed", _NOTHING["single"])


def test_a_mixed_crawl_completes_and_reports_its_empty_pages() -> None:
    make, method, _ = _JOBS["bulk-crawl"]
    mixed = {**_CRAWL_ALL_EMPTY, "ingested": 1, "empty": 2}
    state = _run(make(), method, mixed)
    assert (state.status, state.error) == ("completed", "")
    assert state.results["empty"] == 2
