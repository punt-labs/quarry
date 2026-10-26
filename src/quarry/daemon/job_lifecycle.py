"""A queued job's status moves, each under the job's one claim."""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.daemon.owner_lock import ClaimNotHeldError

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Mapping

    from quarry.daemon.context import DaemonContext
    from quarry.daemon.ingest_unit import IngestUnit
    from quarry.daemon.settlement import Settlement
    from quarry.daemon.tasks import TaskState, TaskStatus

logger = logging.getLogger(__name__)


@final
class JobLifecycle:
    """The only writer of a queued job's ``status``, ``error`` and ``results``.

    The producer waiting for a job, the worker that runs it, the store thread's
    checkpoint, the shutdown abort and the observer of a late store are on
    different threads.  Each move takes the state's one claim and checks what it
    is about to overwrite, so a job is started or withdrawn, committed or
    withdrawn, never both; a status somebody else settled is never replaced; and
    a job past its commit is *not* certain to store (the store deletes the
    earlier copy and then inserts, and the insert can still fail), so the
    producer that gives up on it is told it is :attr:`Withdrawn.COMMITTING`,
    never that it stored.  Every write goes through one funnel that checks the
    claim is held by the writer, and raises if it is not.

    The terminals, the jobs and the abort ask for a move; none assigns a status.
    A task that is not a queued job has its own, simpler rules
    (:class:`~quarry.daemon.task_lifecycle.TaskLifecycle`).
    """

    class Withdrawn(Enum):
        """Where a job stood when its producer withdrew it.

        ``NEVER_STARTED``: it never ran and now never will.  ``STARTED``: it was
        running but had not committed; it will stop at its commit and write
        nothing.  ``COMMITTING``: it is past its commit; it may store, or fail.
        ``STORED`` and ``FAILED``: it had already finished, so the answer is its
        own.
        """

        NEVER_STARTED = auto()
        STARTED = auto()
        COMMITTING = auto()
        STORED = auto()
        FAILED = auto()

    class Disposition(Enum):
        """What a shutdown abort found when it took a job.

        ``FAILED``: the job is now ``failed`` with the abort's reason.  ``KEPT``:
        its producer withdrew it or it stored; it stays what it is.
        ``SETTLED``: its own store ended without storing and said why; that
        refusal stands, and nothing is spooled for it.  ``UNCONFIRMED``: it is
        past its commit and its store has not ended; it stays ``committing``.
        """

        FAILED = auto()
        KEPT = auto()
        SETTLED = auto()
        UNCONFIRMED = auto()

    class Observed(Enum):
        """What the observer of a late store changed in its job.

        ``COMPLETED``: the store landed, so the job is ``completed`` and its
        failure reason is gone.  ``FAILED``: the store of a committing job
        failed, so the job is ``failed``.  ``UNCHANGED``: the status was
        already what the store's end implies, or is not the store's to change.
        """

        COMPLETED = auto()
        FAILED = auto()
        UNCHANGED = auto()

    # A job in one of these is left as it is when a shutdown aborts the queue.
    _KEPT_AT_ABORT: ClassVar[frozenset[TaskStatus]] = frozenset(
        {"withdrawn", "aborted", "completed"}
    )
    # A job in one of these has not started: it may still be withdrawn.
    _WAITING: ClassVar[frozenset[TaskStatus]] = frozenset({"new", "queued"})
    # A job in one of these is the worker's to settle with its terminal outcome.
    _WORKING: ClassVar[frozenset[TaskStatus]] = frozenset({"running", "committing"})

    __slots__ = ("_state",)

    _state: TaskState

    def __new__(cls, state: TaskState) -> Self:
        self = super().__new__(cls)
        self._state = state
        return self

    def enqueue(self) -> bool:
        """Mark the job ``queued``; return False if it was already withdrawn."""
        with self._state.claim:
            if self._state.status == "withdrawn":
                return False
            self._set("queued")
            return True

    def start(self) -> bool:
        """Move a queued job to ``running``; return False if it will not run."""
        with self._state.claim:
            if self._state.status == "queued":
                self._set("running")
                return True
            self._skipped()
            return False

    def commit(self) -> bool:
        """Move a running job past its commit; return False if it was withdrawn."""
        with self._state.claim:
            if self._state.status != "running":
                return False
            self._set("committing")
            return True

    def withdraw(self) -> Withdrawn:
        """Withdraw the job unless it has committed; say where it stood."""
        with self._state.claim:
            status = self._state.status
            if status in self._WAITING:
                self._set("withdrawn")
                status = "withdrawn"
            elif status == "running":
                self._set("aborted")
                status = "aborted"
            # A status this does not know is taken as started: nothing is
            # claimed untouched that might not be.
            return {
                "withdrawn": self.Withdrawn.NEVER_STARTED,
                "committing": self.Withdrawn.COMMITTING,
                "completed": self.Withdrawn.STORED,
                "failed": self.Withdrawn.FAILED,
            }.get(status, self.Withdrawn.STARTED)

    @property
    def kind(self) -> str:
        """Return the kind of the task this lifecycle moves."""
        return self._state.kind

    @property
    def task_id(self) -> str:
        """Return the id of the task this lifecycle moves."""
        return self._state.task_id

    def settle_stored(self, results: Mapping[str, object]) -> bool:
        """Complete the job with *results*; return whether it was.

        A job is completed only while the worker still owns it (running, or
        committing): the producer's withdrawal, and a status an earlier
        correction settled, are never overwritten.  The reason a shutdown abort
        gave it is cleared, so a completed job never carries a failure.
        """
        with self._state.claim:
            if self._state.status not in self._WORKING:
                self._refused("completed")
                return False
            self._complete(results)
            return True

    def settle_failed(self, reason: str) -> bool:
        """Fail the job with *reason*; return whether it was.

        Refused for a job the worker no longer owns: a producer's ``aborted``
        stays ``aborted``, and a job the abort already failed keeps its reason.
        """
        with self._state.claim:
            if self._state.status not in self._WORKING:
                self._refused("failed")
                return False
            self._set("failed", error=reason)
            return True

    def settle(self, settlement: Settlement) -> bool:
        """Settle the job as its store ended; return whether the status moved."""
        if settlement.is_stored:
            return self.settle_stored(settlement.results)
        return self.settle_failed(settlement.reason)

    def cancelled(self, default: str) -> None:
        """Record that the worker's body was cancelled, and hand its store on.

        What is known of the store decides.  Rows already stored: the job is
        ``completed``.  A store that ended with nothing: the job is ``failed``
        with the store's own reason.  A running job whose store has not ended did
        not finish: it is ``failed`` with the reason it already gave, else
        *default*.  A committing job may still store, so it stays committing;
        ``aborted`` and every settled status stay.  The store is then observed
        (:meth:`StoreRun.detach`), so the job is corrected if the thread ends
        later.
        """
        store = self._state.store
        with self._state.claim:
            # Read under the claim: a status the store's own end settles cannot
            # move between the read and the write.
            self._settle_cancelled(
                store.outcome() if store is not None else None, default
            )
        if store is not None:
            store.detach()

    def observe_store(self, settlement: Settlement) -> Observed:
        """Settle a job from its store that ended after its worker was cancelled.

        One critical section.  A ``failed`` or ``committing`` job whose store
        landed is ``completed``, with its reason cleared and its results set; a
        committing job whose store ended with nothing is ``failed`` with the
        store's own reason.  A status the producer or an earlier correction
        settled is left alone.
        """
        with self._state.claim:
            status = self._state.status
            if settlement.is_stored and status in {"failed", "committing"}:
                self._complete(settlement.results)
                return self.Observed.COMPLETED
            if not settlement.is_stored and status == "committing":
                self._set("failed", error=settlement.reason)
                return self.Observed.FAILED
            return self.Observed.UNCHANGED

    def exited(self, default: str) -> None:
        """Record that the worker's body exited with no terminal status of its own.

        A job still ``running`` is failed with the reason it already gave, else
        *default*; ``aborted`` and a committing job whose store runs are not
        "exited without a terminal status", the producer and the store own them.
        """
        with self._state.claim:
            if self._state.status == "running":
                self._fail(default)

    def take_for_abort(self, reason: str) -> Disposition:
        """Fail the job for a shutdown abort the first time it looks at it.

        As :meth:`fail_aborted`, except that a job its own store already failed
        keeps that refusal (:attr:`Disposition.SETTLED`): nothing is spooled for
        a job whose content was looked at and refused.
        """
        store = self._state.store
        with self._state.claim:
            if (
                self._state.status == "failed"
                and store is not None
                and store.outcome() is not None
            ):
                return self.Disposition.SETTLED
        return self.fail_aborted(reason)

    def fail_aborted(self, reason: str) -> Disposition:
        """Fail the job for a shutdown abort, unless it is not the abort's to fail.

        The producer's withdrawal and a job that already stored stay what they
        truthfully are (:attr:`Disposition.KEPT`); one past its commit may still
        store and stays ``committing`` (:attr:`Disposition.UNCONFIRMED`).  Any
        other job is ``failed`` with *reason*, replacing an earlier one.
        """
        with self._state.claim:
            status = self._state.status
            if status in self._KEPT_AT_ABORT:
                return self.Disposition.KEPT
            if status == "committing":
                return self.Disposition.UNCONFIRMED
            self._set("failed", error=reason)
            return self.Disposition.FAILED

    async def run(
        self, job: IngestUnit, ctx: DaemonContext, embed_gate: asyncio.Semaphore
    ) -> None:
        """Run *job* under *embed_gate* unless it was withdrawn.

        The claim is checked before the gate, so a withdrawn job never waits for
        it, and again once the gate is held, so a job withdrawn while it waited
        for the gate is skipped too.  The job records its own terminal state.
        """
        if self._state.status != "queued":
            self._skipped()
            return
        async with embed_gate:  # global embed-concurrency bound
            if self.start():
                await job.run(ctx, self._state)

    def _settle_cancelled(
        self,
        settlement: Settlement | None,  # None: the store has not ended
        default: str,
    ) -> None:
        """Settle a cancelled job from what is known of its store (claim held)."""
        status = self._state.status
        if status not in self._WORKING:
            return
        if settlement is None:
            if status == "running":  # a committing job may still store
                self._fail(default)
        elif settlement.is_stored:
            self._complete(settlement.results)
        else:
            self._set("failed", error=settlement.reason or default)

    def _fail(self, default: str) -> None:
        """Fail with the reason the job gave, else *default* (claim held)."""
        self._set("failed", error=self._state.reason_or(default))

    def _complete(self, results: Mapping[str, object]) -> None:
        """Complete the job with *results* and no failure reason (claim held)."""
        self._set("completed", error="", results=results)

    def _set(
        self,
        status: TaskStatus,
        *,
        error: str | None = None,  # None: the reason stays as it is
        results: Mapping[str, object] | None = None,  # None: the results stay
    ) -> None:
        """Write the job's status (and reason, results), if the claim is held.

        The one place a queued job's fields are assigned.  A caller that does not
        hold the claim has a bug, and it is raised rather than written over.
        """
        if not self._state.claim.owned():
            msg = f"task {self._state.task_id}: written without its claim"
            raise ClaimNotHeldError(msg)
        self._state.status = status
        if error is not None:
            self._state.error = error
        if results is not None:
            self._state.results = dict(results)

    def _refused(self, move: str) -> None:
        """Say, at debug, that a settle did not move the status (claim held)."""
        logger.debug(
            "job %s: not %s, its status is %s",
            self._state.task_id,
            move,
            self._state.status,
        )

    def _skipped(self) -> None:
        """Warn of a job not started for any reason but being withdrawn."""
        if self._state.status != "withdrawn":
            logger.warning("job not started: its status is %s", self._state.status)
