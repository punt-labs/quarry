"""A real queue, jobs and spool, with stores and spool writes the test releases."""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self, cast, final
from unittest.mock import patch

from quarry.daemon.abort_tally import AbortTally
from quarry.daemon.backfill_job import BackfillTranscriptJob
from quarry.daemon.content_jobs import ScrubbedIngestJob
from quarry.daemon.index_jobs import CollectionSyncJob, DocumentDeleteJob, FileIndexJob
from quarry.daemon.ingest_labels import IngestLabels
from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.job_lifecycle import JobLifecycle
from quarry.daemon.job_spool import JobSpool, SpoolRecord
from quarry.daemon.queued_job import JobWithdrawnError, QueuedJob
from quarry.daemon.route_key import RouteKey
from quarry.daemon.tasks import TaskRegistry, TaskState
from quarry.ingestion.file_indexer import FileIndexOutcome
from quarry.shutdown_policy import ShutdownPolicy
from quarry.sync import SyncResult

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from quarry.config import Settings
    from quarry.daemon.collection_worker import CollectionWorker
    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.db import Database

# Every write to a watched status, error or results field: the job, the field,
# whether the writer held the job's claim, and the module that wrote.
WRITES: list[tuple[str, str, bool, str]] = []
_WATCHED = frozenset({"status", "error", "results"})


@dataclass(slots=True)
class WatchedState(TaskState):
    """A task state that records who wrote its status, error or results."""

    def __setattr__(self, name: str, value: object) -> None:
        if name in _WATCHED:
            try:
                owned = self.claim.owned()
            except AttributeError:
                owned = True  # the dataclass's own construction, before any claim
            else:
                module = Path(sys._getframe(1).f_code.co_filename).name
                WRITES.append((self.task_id, name, owned, module))
        object.__setattr__(self, name, value)


@final
@dataclass(slots=True)
class Store:
    """One job's store thread: it starts, waits to be released, then ends."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    ended: threading.Event = field(default_factory=threading.Event)
    outcome: object = field(default_factory=lambda: {"chunks": 3})

    def land(self, outcome: object = None) -> None:
        """Let the store end with *outcome* (rows by default)."""
        if outcome is not None:
            self.outcome = outcome
        self.release.set()


@final
@dataclass(slots=True)
class SpoolGate:
    """One job's spool write: it starts and waits to be released."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)


@final
@dataclass(frozen=True, slots=True)
class Entry:
    """A job admitted to the queue, with the state it reports through."""

    name: str
    job: IngestUnit
    state: WatchedState
    store: Store


def checkpoint(state: TaskState, message: str) -> None:
    """Claim the commit as the backfill's writer does; raise if withdrawn."""
    if message == QueuedJob.COMMIT_POINT and not JobLifecycle(state).commit():
        msg = "job withdrawn before its commit"
        raise JobWithdrawnError(msg)


_TERMS = {
    "spooled": r"(\d+) spooled for recovery",
    "unrecoverable": r"(\d+) spooled but not recoverable",
    "not_written": r"(\d+) not written",
    "unbuilt": r"(\d+) unbuilt",
    "unfinished": r"(\d+) spool unfinished",
    "no_spool": r"(\d+) needed no spool",
    "kept": r"(\d+) kept",
    "unconfirmed": r"(\d+) unconfirmed",
}


SETTLED = frozenset({"completed", "failed", "withdrawn", "aborted"})

TERM_PARTS = (
    "spooled",
    "unrecoverable",
    "not_written",
    "unbuilt",
    "unfinished",
    "no_spool",
    "kept",
    "unconfirmed",
)


def terms(summary: str) -> dict[str, int]:
    """Return the count of each term in a tally *summary*; a term it lacks is zero."""
    found = {}
    for key, pattern in _TERMS.items():
        match = re.search(pattern, summary)
        found[key] = int(match.group(1)) if match else 0
    head = re.match(r"(\d+) job", summary)
    assert head is not None
    found["jobs"] = int(head.group(1))
    return found


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait on the loop until *predicate* holds, or fail."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            msg = "condition never held"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


@final
class World:
    """A daemon context whose stores and spool writes the test controls."""

    __slots__ = (
        "_ctx",
        "_entries",
        "_gates",
        "_namespace",
        "_real_write",
        "_root",
        "_stack",
        "_stores",
    )

    _ctx: DaemonContext
    _entries: dict[str, Entry]
    _gates: dict[str, SpoolGate]
    _namespace: SimpleNamespace
    _real_write: Callable[[JobSpool, SpoolRecord], Path | None]
    _root: Path
    _stack: ExitStack
    _stores: dict[str, Store]

    def __new__(cls, tmp_path: Path) -> Self:
        self = super().__new__(cls)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self._root = Path(tempfile.mkdtemp(dir=tmp_path))
        settings = SimpleNamespace(
            ingest_embed_concurrency=16,
            ingest_queue_depth=64,
            ingest_drain_timeout_s=30.0,
            ingest_max_workers=32,
            ingest_worker_idle_s=60.0,
            quarry_root=self._root,
            registry_path=tmp_path / "registry.db",
        )
        self._namespace = SimpleNamespace(
            settings=settings,
            database_name="db",
            database=object(),
            tasks=TaskRegistry(),
        )
        self._ctx = cast("DaemonContext", self._namespace)
        self._stores = {}
        self._gates = {}
        self._entries = {}
        self._stack = ExitStack()
        self._real_write = JobSpool.write
        return self

    @property
    def ctx(self) -> DaemonContext:
        """Return the daemon context the queue runs in."""
        return self._ctx

    def __enter__(self) -> Self:
        for active in (
            # Most scenarios hold one store per collection at once; with the real
            # ceiling of one, only the first would ever start.
            patch("quarry.daemon.ingest_queue.EMBED_CONCURRENCY_CEILING", 16),
            patch.object(IngestQueue, "ABORT_WAIT_S", 2.0),
            patch.object(ShutdownPolicy, "SPOOL_WAIT_S", 0.3),
            patch.object(
                ScrubbedIngestJob, "scrub_and_ingest", self._ingest_function()
            ),
            patch.object(JobSpool, "write", self._spool_function()),
        ):
            self._stack.enter_context(active)
        self._namespace.ingest_queue = IngestQueue(self._ctx)
        return self

    def __exit__(self, *exc: object) -> None:
        for store in self._stores.values():
            store.release.set()
        for gate in self._gates.values():
            gate.release.set()
        self._stack.close()

    def admit(
        self,
        name: str,
        *,
        collection: str = "c1",
        backfill: bool = False,
        commits: bool = False,
    ) -> Entry:
        """Admit a ``remember`` job (or a backfill transcript) the way a route does."""
        state = WatchedState(task_id=name, kind="remember")
        labels = IngestLabels(
            overwrite=True,
            collection=collection,
            agent_handle="",
            memory_type="",
            summary="",
        )
        inline = ScrubbedIngestJob(
            name=name,
            content="text",
            format_hint="auto",
            scrub_label="remember",
            labels=labels,
            checkpoint=partial(checkpoint, state) if commits else None,
        )
        job: IngestUnit = BackfillTranscriptJob(inline) if backfill else inline
        self._stores[name] = store = Store()
        entry = Entry(name, job, state, store)
        self._entries[name] = entry
        key = RouteKey("db", collection)
        assert self._ctx.ingest_queue.try_submit(key, job, state)
        return entry

    def break_spool(self, blocker: Path) -> None:
        """Make the spool directory impossible to create: *blocker* is a file."""
        self._namespace.settings.quarry_root = blocker
        self._root = blocker

    def admit_index_jobs(self, monkeypatch: pytest.MonkeyPatch) -> list[Entry]:
        """Admit a file index, a delete and a sync job, each held in its own store."""
        entries = []
        specs = (
            ("index", FileIndexJob, "_index", FileIndexOutcome("doc", 1, None)),
            ("delete", DocumentDeleteJob, "_delete", 1),
            ("sync", CollectionSyncJob, "_sync", SyncResult("c", 1, 0, 0, 0, 0)),
        )
        for n, (name, cls, method, result) in enumerate(specs):
            collection = f"i{n}"
            monkeypatch.setattr(cls, method, self._held(name))
            job = self._index_job(cls, collection)
            state = WatchedState(task_id=name, kind=name, status="new")
            self._stores[name] = store = Store(outcome=result)
            entry = Entry(name, job, state, store)
            self._entries[name] = entry
            key = RouteKey("db", collection)
            assert self._ctx.ingest_queue.try_submit(key, job, state)
            entries.append(entry)
        return entries

    def _held(self, name: str) -> Callable[[object], object]:
        def call(job: object) -> object:
            del job
            store = self._stores[name]
            store.started.set()
            store.release.wait(30)
            store.ended.set()
            return store.outcome

        return call

    @staticmethod
    def _index_job(kind: type, collection: str) -> IngestUnit:
        database = cast("Database", object())
        settings = cast("Settings", object())
        root = Path("/r")
        if kind is FileIndexJob:
            return FileIndexJob(database, settings, collection, root, root / "f")
        if kind is DocumentDeleteJob:
            return DocumentDeleteJob(database, settings, collection, ("a",))
        return CollectionSyncJob(database, settings, collection, root)

    def hold_spool(self, name: str) -> SpoolGate:
        """Make the spool write of the job named *name* wait to be released."""
        self._gates[name] = SpoolGate()
        return self._gates[name]

    def worker(self, collection: str = "c1") -> CollectionWorker:
        """Return the worker that runs *collection*."""
        worker = self._ctx.ingest_queue.pool.for_key(RouteKey("db", collection))
        assert worker is not None, f"no worker for {collection}"
        return worker

    def workers(self) -> list[CollectionWorker]:
        """Return every resident worker."""
        return self._ctx.ingest_queue.pool.workers()

    def terms(self) -> dict[str, int]:
        """Return the tally's counts, after checking they add up to the jobs taken."""
        found = terms(self.summary())
        assert sum(found[p] for p in TERM_PARTS) == found["jobs"], self.summary()
        return found

    def summary(self) -> str:
        """Return the tally of every worker, as the abort's log line reports it."""
        return AbortTally.total(w.tally for w in self.workers()).summary()

    def pending(self) -> int:
        """Return how many taken jobs have no recorded outcome."""
        return sum(w.pending_jobs for w in self.workers())

    def spool_files(self) -> list[Path]:
        """Return the spool records on disk."""
        return sorted((self._root / "spool").glob("remember-*.json"))

    async def abort(self) -> None:
        """Close the queue with a drain that has already run out."""
        await self._ctx.ingest_queue.aclose(drain_timeout=0.05)

    async def landed(self, *entries: Entry) -> None:
        """Wait until each store thread has ended and the loop has heard of it."""
        for entry in entries:
            await until(entry.store.ended.is_set)
        await asyncio.sleep(0.05)

    async def running(self, *names: str) -> None:
        """Wait until each named job's store thread is running."""
        for name in names:
            await until(self._stores[name].started.is_set)

    def _ingest_function(
        self,
    ) -> Callable[[ScrubbedIngestJob, DaemonContext], dict[str, object]]:
        return lambda job, ctx: self._ingest(job, ctx)

    def _spool_function(self) -> Callable[[JobSpool, SpoolRecord], object]:
        return lambda spool, record: self._spool(spool, record)

    def _ingest(self, job: ScrubbedIngestJob, ctx: DaemonContext) -> dict[str, object]:
        del ctx
        store = self._stores[job.name]
        try:
            if job.checkpoint is not None:
                job.checkpoint(QueuedJob.COMMIT_POINT)
            store.started.set()
            store.release.wait(30)
            if isinstance(store.outcome, BaseException):
                raise store.outcome
            return dict(cast("dict[str, object]", store.outcome))
        finally:
            store.ended.set()

    def _spool(self, spool: JobSpool, record: SpoolRecord) -> object:
        if (gate := self._gates.get(record.name)) is not None:
            gate.started.set()
            gate.release.wait(30)
        return self._real_write(spool, record)
