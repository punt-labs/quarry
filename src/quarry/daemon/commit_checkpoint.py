"""The last point a withdrawn job can stop: just before it deletes or stores."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Self, final

from quarry.refusal import RefusalError

if TYPE_CHECKING:
    from quarry.daemon.job_withdrawal import JobWithdrawal


class JobWithdrawnError(RefusalError):
    """The job's producer withdrew it before it committed; nothing was written."""


@final
class CommitCheckpoint:
    """A progress callback that stops a withdrawn job at its commit.

    The ingest pipeline reports :attr:`COMMIT_POINT` once it holds its chunks and
    before it replaces the earlier document or inserts anything.  Reaching it
    claims the commit: a job whose producer already withdrew it raises, so
    nothing is deleted or stored; a job that gets past it can no longer be
    withdrawn and is reported stored.  ``tests/test_commit_checkpoint.py`` pins
    the pipeline's message and its place before the delete.
    """

    COMMIT_POINT: ClassVar[str] = "Embedding + storing in bounded windows"

    __slots__ = ("_claim",)

    _claim: JobWithdrawal

    def __new__(cls, claim: JobWithdrawal) -> Self:
        self = super().__new__(cls)
        self._claim = claim
        return self

    def __call__(self, message: str) -> None:
        """Claim the commit when *message* is the commit point; raise if withdrawn."""
        if message == self.COMMIT_POINT and not self._claim.commit():
            msg = "job withdrawn before its commit"
            raise JobWithdrawnError(msg)
