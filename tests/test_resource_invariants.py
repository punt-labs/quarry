"""Resource-invariant tier: a long-lived connection must not leak descriptors.

The daemon holds a LanceDB connection for its whole lifetime and rebuilds the
FTS/scalar index on every sync. Each ``create_fts_index(replace=True)`` supersedes
an index generation and deletes the old files, but LanceDB's Rust core keeps the
readers open — one leaked descriptor per generation — until the process hits
``RLIMIT_NOFILE`` and ``quarry find`` starts returning HTTP 500. A short-lived CLI
never notices; only a long-lived process accumulates.

This test stands in for the daemon: one connection, many optimize cycles. On the
leaking code the open-fd count rises monotonically; the fix recycles the connection
so the count plateaus. The soft fd limit is raised to the hard limit for the
duration so the leaking path fails as a clean assertion rather than an ``EMFILE``
crash mid-run.
"""

from __future__ import annotations

import gc
import resource
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self

import numpy as np
import pytest

from quarry.db import Database
from quarry.models import Chunk

if TYPE_CHECKING:
    from collections.abc import Generator

    from numpy.typing import NDArray

pytestmark = pytest.mark.resource

_ITERATIONS = 120
_CHUNKS_PER_ITERATION = 3
_EMBEDDING_DIM = 768
# Dense per-iteration sampling averages the recycle sawtooth out of the quartile
# means, so a bounded oscillation reads as flat while a real leak reads as a ramp.
# The slack sits far below the leaking path's growth (~2 fds/iteration, hundreds
# over the run) yet above one recycle amplitude, so the invariant is decisive.
_PLATEAU_SLACK = 40


def _open_fd_count() -> int:
    """Return the number of file descriptors this process currently holds."""
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        path = Path(fd_dir)
        if path.is_dir():
            return sum(1 for _ in path.iterdir())
    msg = "no /proc/self/fd or /dev/fd on this platform"
    raise RuntimeError(msg)


class FdTrajectory:
    """The sequence of open-fd counts sampled across a workload.

    Answers the one question the invariant cares about: does descriptor usage
    plateau (bounded), or climb (leak)? Plateau is defined by comparing the mean
    of the final quartile against the mean of the first quartile.
    """

    __slots__ = ("_samples",)

    _samples: list[int]

    def __new__(cls) -> Self:
        self = super().__new__(cls)
        self._samples = []
        return self

    def record(self, count: int) -> None:
        """Append one sampled descriptor count."""
        self._samples.append(count)

    @property
    def peak(self) -> int:
        """The highest descriptor count observed."""
        return max(self._samples)

    def _quartile_mean(self, *, last: bool) -> float:
        n = len(self._samples)
        size = max(1, n // 4)
        window = self._samples[-size:] if last else self._samples[:size]
        return sum(window) / len(window)

    def growth(self) -> float:
        """Return final-quartile mean minus first-quartile mean."""
        return self._quartile_mean(last=True) - self._quartile_mean(last=False)

    def plateaus(self, *, slack: int) -> bool:
        """Whether the trajectory is bounded within *slack* of its baseline."""
        return self.growth() <= slack


def _make_chunks(iteration: int) -> list[Chunk]:
    now = datetime.now(tz=UTC)
    return [
        Chunk(
            document_name=f"doc-{iteration}",
            document_path=f"/virtual/doc-{iteration}.txt",
            collection="default",
            page_number=1,
            total_pages=1,
            chunk_index=i,
            text=f"resource invariant probe {iteration}-{i} lorem ipsum dolor sit amet",
            page_raw_text="raw",
            page_type="text",
            source_format="txt",
            ingestion_timestamp=now,
        )
        for i in range(_CHUNKS_PER_ITERATION)
    ]


def _random_vectors(n: int) -> NDArray[np.float32]:
    rng = np.random.default_rng(0)
    return rng.standard_normal((n, _EMBEDDING_DIM)).astype(np.float32)


@pytest.fixture()
def _raised_fd_limit() -> Generator[None]:
    """Raise the soft fd limit to the hard limit so a leak fails as an assertion.

    Without this, the leaking code path exhausts the default soft limit and
    crashes with ``EMFILE`` partway through, obscuring the measured trajectory.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


# `_ITERATIONS` optimize cycles brush the 30s default timeout; this tier is
# legitimately long, so give it explicit headroom rather than shrinking it into
# flakiness.
@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_raised_fd_limit")
def test_optimize_loop_does_not_leak_descriptors(tmp_path: Path) -> None:
    """A single connection optimized repeatedly must not leak file descriptors.

    Stands in for the daemon, including its reclamation regime: recycling drops
    the superseded connection but the lancedb binding holds it in a reference
    cycle, so the descriptors are freed by cyclic GC, not refcounting. The daemon
    gets that GC from ``SyncFinalizer.gc.collect(2)`` after every sync; this test
    calls raw ``optimize`` so it runs the collection itself each iteration. Without
    recycling (the leaking path) the readers stay pinned by the live connection
    and no GC can reclaim them, so the assertion still fails on unfixed code.
    """
    database = Database.connect(tmp_path / "lancedb")
    trajectory = FdTrajectory()

    for iteration in range(_ITERATIONS):
        database.store.insert(
            _make_chunks(iteration), _random_vectors(_CHUNKS_PER_ITERATION)
        )
        database.optimizer.optimize(force=True)
        gc.collect()  # model SyncFinalizer's post-sync collection (sync_finalize.py)
        trajectory.record(_open_fd_count())

    assert trajectory.plateaus(slack=_PLATEAU_SLACK), (
        f"open fds grew by {trajectory.growth():.1f} across {_ITERATIONS} optimize "
        f"cycles (peak {trajectory.peak}); descriptor leak in the connection layer"
    )
