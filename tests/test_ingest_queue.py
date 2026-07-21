"""Concurrency, backpressure, ordering, and lifecycle tests for the ingest queue.

The queue is driven by an instrumented :class:`_StubUnit` that records the
embed-section concurrency into a shared :class:`_Ledger`, so the invariants can
be asserted without a real ONNX model or LanceDB (the daemon-mandatory tests
stay hermetic).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Self, cast, final

from quarry.daemon import ingest_queue as iq
from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.job_spool import SpoolRecord
from quarry.daemon.routes.base import RouteGroup
from quarry.daemon.tasks import TaskRegistry, TaskState, task_terminal

if TYPE_CHECKING:
    from pytest import MonkeyPatch

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit


@final
class _Ledger:
    """Records embed-section concurrency and completion order across stub units."""

    __slots__ = ("_active", "_per", "completions", "peak", "peak_per")

    _active: int
    _per: dict[str, int]
    completions: list[str]
    peak: int
    peak_per: dict[str, int]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._active = 0
        self._per = defaultdict(int)
        self.completions = []
        self.peak = 0
        self.peak_per = defaultdict(int)
        return self

    def enter(self, collection: str) -> None:
        """Mark a unit entering the embed section, tracking peak concurrency."""
        self._active += 1
        self.peak = max(self.peak, self._active)
        self._per[collection] += 1
        current = self._per[collection]
        self.peak_per[collection] = max(self.peak_per[collection], current)

    def leave(self, collection: str, name: str) -> None:
        """Mark a unit leaving the embed section, recording completion order."""
        self._active -= 1
        self._per[collection] -= 1
        self.completions.append(name)


@final
@dataclass(frozen=True, slots=True)
class _StubUnit:
    """An ``IngestUnit`` that reports concurrency and can block, delay, or fail."""

    collection: str
    name: str
    ledger: _Ledger
    delay: float = 0.0
    gate: asyncio.Event | None = None
    barrier: asyncio.Barrier | None = None
    boom: bool = False
    spool: SpoolRecord | None = None

    async def run(self, ctx: DaemonContext, state: TaskState) -> None:
        """Record entry, wait as configured, then record a terminal status."""
        del ctx  # the stub ignores the daemon context; the queue passes it through
        with task_terminal(state):
            self.ledger.enter(self.collection)
            try:
                if self.gate is not None:
                    await self.gate.wait()
                if self.barrier is not None:
                    await self.barrier.wait()
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.boom:
                    msg = "stub failure"
                    raise RuntimeError(msg)
                state.status = "completed"
            finally:
                self.ledger.leave(self.collection, self.name)

    def spool_record(self) -> SpoolRecord | None:
        """Return the configured spool record (``None`` for durable-copy jobs)."""
        return self.spool


@final
@dataclass(frozen=True, slots=True)
class _StubQueue:
    """A queue double for route tests: ``try_submit`` returns a canned verdict."""

    admit: bool
    calls: list[tuple[str, str]] = field(default_factory=list)

    def try_submit(self, collection: str, _job: object, state: TaskState) -> bool:
        """Record the call and return the canned admission verdict."""
        self.calls.append((collection, state.task_id))
        return self.admit


def _make_queue(
    *,
    depth: int = 32,
    concurrency: int = 1,
    max_workers: int = 256,
    worker_idle: float = 60.0,
    quarry_root: Path | None = None,
) -> IngestQueue:
    """Build an ``IngestQueue`` over a fake context carrying only settings."""
    settings = SimpleNamespace(
        ingest_embed_concurrency=concurrency,
        ingest_queue_depth=depth,
        ingest_drain_timeout_s=30.0,
        ingest_max_workers=max_workers,
        ingest_worker_idle_s=worker_idle,
        quarry_root=quarry_root or Path(tempfile.mkdtemp()),
    )
    ctx = SimpleNamespace(settings=settings)
    return IngestQueue(cast("DaemonContext", ctx))


def _state(name: str) -> TaskState:
    return TaskState(task_id=name, kind="capture")


def _lift_ceiling(monkeypatch: MonkeyPatch, value: int) -> None:
    """Raise the embed-concurrency ceiling so the worker (not the gate) limits."""
    monkeypatch.setattr(iq, "EMBED_CONCURRENCY_CEILING", value)


def test_same_collection_runs_one_writer_at_a_time(monkeypatch: MonkeyPatch) -> None:
    """N jobs on one collection never overlap, even when the embed gate allows it.

    The ceiling is lifted so the shared embed semaphore is *not* the limiter;
    the single per-collection worker is what serializes, proving job-level
    serialization is independent of the embed bound.
    """
    _lift_ceiling(monkeypatch, 4)

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=4)
        for i in range(5):
            assert queue.try_submit(
                "solo", _StubUnit("solo", f"j{i}", ledger, delay=0.01), _state(f"j{i}")
            )
        await queue.aclose(drain_timeout=2.0)
        assert ledger.peak_per["solo"] == 1
        assert ledger.completions == [f"j{i}" for i in range(5)]  # FIFO

    asyncio.run(_run())


def test_different_collections_run_concurrently(monkeypatch: MonkeyPatch) -> None:
    """With the gate lifted, one job per collection embeds in parallel."""
    _lift_ceiling(monkeypatch, 4)

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=4)
        barrier = asyncio.Barrier(4)
        for i in range(4):
            col = f"c{i}"
            assert queue.try_submit(
                col, _StubUnit(col, col, ledger, barrier=barrier), _state(col)
            )
        await queue.aclose(drain_timeout=2.0)
        assert ledger.peak == 4  # all four inside the embed section together
        assert len(ledger.completions) == 4

    asyncio.run(_run())


def test_embed_gate_bounds_total_in_flight_at_default() -> None:
    """At the shipped ceiling (1), at most one job embeds anywhere, all complete."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=1)
        for i in range(4):
            col = f"c{i}"
            assert queue.try_submit(
                col, _StubUnit(col, col, ledger, delay=0.01), _state(col)
            )
        await queue.aclose(drain_timeout=2.0)
        assert ledger.peak == 1  # global embed bound holds across collections
        assert len(ledger.completions) == 4

    asyncio.run(_run())


def test_full_queue_rejects_without_dropping() -> None:
    """Admission past ``MAX_QUEUE_DEPTH`` returns False; a freed slot re-admits."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(depth=2, concurrency=1)
        gate = asyncio.Event()
        first = _StubUnit("c", "first", ledger, gate=gate)
        second = _StubUnit("c", "second", ledger, gate=gate)
        assert queue.try_submit("c", first, _state("first"))  # depth 1 (in flight)
        assert queue.try_submit("c", second, _state("second"))  # depth 2 (waiting)
        await asyncio.sleep(0.02)  # let the worker pick up "first"
        overflow = _StubUnit("c", "overflow", ledger, gate=gate)
        assert not queue.try_submit("c", overflow, _state("overflow"))  # full -> 503
        gate.set()
        await queue.aclose(drain_timeout=2.0)
        assert set(ledger.completions) == {"first", "second"}  # overflow never ran

    asyncio.run(_run())


def test_hot_collection_does_not_starve_a_cold_one() -> None:
    """A deep backlog on one collection does not delay a lone cold-collection job."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=1)
        for i in range(6):
            assert queue.try_submit(
                "hot", _StubUnit("hot", f"hot{i}", ledger, delay=0.01), _state(f"h{i}")
            )
        assert queue.try_submit(
            "cold", _StubUnit("cold", "cold", ledger, delay=0.01), _state("cold")
        )
        await queue.aclose(drain_timeout=3.0)
        cold_index = ledger.completions.index("cold")
        # The cold job finishes well before the whole hot backlog drains — the
        # shared gate grants to FIFO waiters, so the cold worker takes a turn.
        assert cold_index < 5

    asyncio.run(_run())


def test_status_moves_queued_then_running_then_completed() -> None:
    """A job is observed queued on admit, running on dequeue, then completed."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=1)
        gate = asyncio.Event()
        state = _state("job")
        assert queue.try_submit("c", _StubUnit("c", "job", ledger, gate=gate), state)
        assert state.status == "queued"
        await asyncio.sleep(0.02)  # worker dequeues and flips to running
        assert state.status == "running"
        gate.set()
        await queue.aclose(drain_timeout=2.0)
        assert state.status == "completed"

    asyncio.run(_run())


def test_raising_job_records_failed_without_crashing_worker() -> None:
    """A unit that raises surfaces ``failed`` and does not kill the worker loop."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=1)
        boom = _state("boom")
        ok = _state("ok")
        assert queue.try_submit("c", _StubUnit("c", "boom", ledger, boom=True), boom)
        assert queue.try_submit("c", _StubUnit("c", "ok", ledger), ok)
        await queue.aclose(drain_timeout=2.0)
        assert boom.status == "failed"
        assert ok.status == "completed"  # worker survived the raise and kept draining

    asyncio.run(_run())


def test_shutdown_drains_queued_jobs() -> None:
    """A clean shutdown flushes every queued job to a terminal status."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=1)
        states = [_state(f"j{i}") for i in range(5)]
        for i, state in enumerate(states):
            assert queue.try_submit(
                "c", _StubUnit("c", f"j{i}", ledger, delay=0.01), state
            )
        await queue.aclose(drain_timeout=3.0)
        assert all(s.status == "completed" for s in states)
        assert len(ledger.completions) == 5

    asyncio.run(_run())


def test_shutdown_timeout_fails_remaining_jobs() -> None:
    """On a forced drain timeout, unfinished jobs record ``failed`` (recoverable)."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=1)
        gate = asyncio.Event()  # never set -> jobs block past the drain timeout
        states = [_state(f"j{i}") for i in range(3)]
        for i, state in enumerate(states):
            assert queue.try_submit(
                "c", _StubUnit("c", f"j{i}", ledger, gate=gate), state
            )
        await queue.aclose(drain_timeout=0.1)
        assert all(s.status == "failed" for s in states)

    asyncio.run(_run())


def test_route_submit_rejects_full_queue_with_503_and_no_task() -> None:
    """``RouteGroup.submit`` maps a full queue to 503 and drops the task record."""
    tasks = TaskRegistry()
    state = tasks.begin("capture")
    queue = _StubQueue(admit=False)
    ctx = SimpleNamespace(ingest_queue=queue, tasks=tasks)
    group = RouteGroup(cast("DaemonContext", ctx))

    resp = group.submit(cast("IngestUnit", SimpleNamespace(collection="c")), state)

    assert resp.status_code == 503
    assert state.task_id not in tasks  # dropped -> no orphan stuck in "queued"


def test_route_submit_accepts_with_byte_identical_202_body() -> None:
    """A successful submit returns the unchanged 202 accepted-body (wire parity)."""
    tasks = TaskRegistry()
    state = tasks.begin("capture")
    queue = _StubQueue(admit=True)
    ctx = SimpleNamespace(ingest_queue=queue, tasks=tasks)
    group = RouteGroup(cast("DaemonContext", ctx))

    resp = group.submit(cast("IngestUnit", SimpleNamespace(collection="c")), state)

    assert resp.status_code == 202
    assert json.loads(bytes(resp.body)) == {
        "task_id": state.task_id,
        "status": "accepted",
    }
    assert queue.calls == [("c", state.task_id)]


def test_embed_concurrency_clamped_to_ceiling() -> None:
    """A configured concurrency above the ceiling is clamped, never honored raw."""
    settings = SimpleNamespace(
        ingest_embed_concurrency=99,
        ingest_queue_depth=32,
        ingest_drain_timeout_s=30.0,
        ingest_max_workers=256,
        ingest_worker_idle_s=60.0,
    )
    ctx = cast("DaemonContext", SimpleNamespace(settings=settings))
    assert IngestQueue._embed_concurrency(ctx) == iq.EMBED_CONCURRENCY_CEILING


def test_drain_abort_fails_a_job_blocked_on_the_embed_gate() -> None:
    """A job cancelled while awaiting the gate records failed, not stuck running."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(concurrency=1)
        held = asyncio.Event()  # never set -> job A holds the embed gate forever
        a = _state("a")
        b = _state("b")
        assert queue.try_submit("cA", _StubUnit("cA", "a", ledger, gate=held), a)
        await asyncio.sleep(0.02)  # A's worker takes the gate and enters job.run
        assert queue.try_submit("cB", _StubUnit("cB", "b", ledger), b)
        await asyncio.sleep(0.02)  # B's worker dequeues and blocks awaiting the gate
        assert b.status == "running"  # dequeued + gate-blocked (the #7 gap)
        await queue.aclose(drain_timeout=0.05)  # drain times out -> abort
        assert b.status == "failed"  # gate-blocked job now reaches a terminal state
        assert a.status == "failed"

    asyncio.run(_run())


def test_idle_workers_are_reaped_so_client_keys_cannot_accrue() -> None:
    """Distinct collection names do not grow the worker map without bound.

    With a zero idle interval, each submit reaps the previous now-idle worker, so
    a caller sending one job per distinct collection never accumulates resident
    workers.  A reaped collection is recreated on demand and still completes.
    """

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(worker_idle=0.0)
        for i in range(6):
            collection = f"c{i}"
            assert queue.try_submit(
                collection, _StubUnit(collection, collection, ledger), _state(f"j{i}")
            )
            await asyncio.sleep(0.02)  # let the worker finish and go idle
            assert len(queue._workers) <= 1  # prior idle worker reaped on next submit
        await queue.aclose(drain_timeout=2.0)
        assert len(ledger.completions) == 6

    asyncio.run(_run())


def test_reaped_worker_task_is_retained_then_discarded() -> None:
    """A reaped worker's cancelled task is held until it finishes, never stranded.

    asyncio keeps only a weak reference to a task, so dropping a reaped worker
    without retaining its still-pending task risks 'Task was destroyed but it is
    pending'. The queue retains it in ``_reaped`` and the done-callback discards
    it once the cancellation runs out.
    """

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(worker_idle=0.0)
        assert queue.try_submit("c0", _StubUnit("c0", "c0", ledger), _state("j0"))
        await asyncio.sleep(0.02)  # c0 finishes its job and goes idle
        assert queue.try_submit("c1", _StubUnit("c1", "c1", ledger), _state("j1"))
        assert queue._reaped  # the reaped c0 task is retained, not garbage
        await asyncio.sleep(0.02)  # let the cancellation complete
        assert not queue._reaped  # done-callback discarded the finished task
        await queue.aclose(drain_timeout=2.0)

    asyncio.run(_run())


def test_cancel_workers_retains_aborted_tasks_until_done() -> None:
    """cancel_workers holds each aborted worker's task until its cancel completes."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue()
        gate = asyncio.Event()  # never set -> the job blocks until aborted
        unit = _StubUnit("c", "c", ledger, gate=gate)
        assert queue.try_submit("c", unit, _state("j"))
        await asyncio.sleep(0.02)  # worker dequeues and runs the blocked job
        queue.cancel_workers()
        assert queue._reaped  # the aborted task is retained
        await asyncio.sleep(0.02)  # cancellation runs out
        assert not queue._reaped  # done-callback discarded it

    asyncio.run(_run())


def test_aborted_queued_job_without_durable_copy_is_spooled(tmp_path: Path) -> None:
    """A drain-aborted remember/ingest is spooled, never silently dropped.

    quarry-atsz: job A holds the worker (blocked) so B stays queued; the drain
    times out and aborts. B has no durable client copy, so it must be recorded
    in the server-side spool with its recoverable content, and its task records
    the spooled reason rather than a silent 'failed'.
    """

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(quarry_root=tmp_path)
        gate = asyncio.Event()  # never set -> A blocks past the drain timeout
        a = _StubUnit("c", "a", ledger, gate=gate)
        record = SpoolRecord("remember", "c", "note", "remembered content")
        b = _StubUnit("c", "b", ledger, gate=gate, spool=record)
        state_b = _state("b")
        assert queue.try_submit("c", a, _state("a"))
        await asyncio.sleep(0.02)  # A dequeued and blocked; B enqueued behind it
        assert queue.try_submit("c", b, state_b)
        await queue.aclose(drain_timeout=0.05)  # times out -> abort
        assert state_b.status == "failed"
        assert "spooled" in state_b.error
        files = list((tmp_path / "spool").glob("remember-*.json"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text())
        assert payload["collection"] == "c"
        assert payload["payload"] == "remembered content"

    asyncio.run(_run())


def test_aborted_in_flight_job_without_durable_copy_is_spooled(tmp_path: Path) -> None:
    """The single in-flight job (not in the FIFO) is spooled on a hard abort.

    quarry-atsz: the already-dequeued job is tracked as ``_current``; a hard
    drain-timeout abort must spool it too, else the one in-flight remember/ingest
    is silently lost even though the queued ones are recovered.
    """

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(quarry_root=tmp_path)
        gate = asyncio.Event()  # never set -> the job stays in flight, blocked
        record = SpoolRecord("remember", "c", "note", "in-flight content")
        unit = _StubUnit("c", "j", ledger, gate=gate, spool=record)
        state = _state("j")
        assert queue.try_submit("c", unit, state)
        await asyncio.sleep(0.02)  # dequeued -> in flight, blocked on the gate
        await queue.aclose(drain_timeout=0.05)  # times out -> abort
        assert state.status == "failed"
        files = list((tmp_path / "spool").glob("remember-*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text())["payload"] == "in-flight content"

    asyncio.run(_run())


def test_aborted_queued_job_with_durable_copy_is_not_spooled(tmp_path: Path) -> None:
    """A capture (durable .md) aborted at drain fails without a spool file."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(quarry_root=tmp_path)
        gate = asyncio.Event()
        a = _StubUnit("c", "a", ledger, gate=gate)
        b = _StubUnit("c", "b", ledger, gate=gate)  # spool=None -> durable copy
        state_b = _state("b")
        assert queue.try_submit("c", a, _state("a"))
        await asyncio.sleep(0.02)
        assert queue.try_submit("c", b, state_b)
        await queue.aclose(drain_timeout=0.05)
        assert state_b.status == "failed"
        assert not list((tmp_path / "spool").glob("*.json"))

    asyncio.run(_run())


def test_worker_cap_rejects_when_all_workers_busy() -> None:
    """At the worker cap with every collection busy, a new key is shed with 503."""

    async def _run() -> None:
        ledger = _Ledger()
        queue = _make_queue(max_workers=2)
        gate = asyncio.Event()
        for col in ("c0", "c1"):
            unit = _StubUnit(col, col, ledger, gate=gate)
            assert queue.try_submit(col, unit, _state(col))
        await asyncio.sleep(0.02)  # both workers dequeue and become busy
        # Cap is full and neither worker is reapable -> no admission (route 503s).
        overflow = _StubUnit("c2", "c2", ledger, gate=gate)
        assert not queue.try_submit("c2", overflow, _state("c2"))
        gate.set()
        await queue.aclose(drain_timeout=2.0)
        assert set(ledger.completions) == {"c0", "c1"}

    asyncio.run(_run())
