"""Resource-invariant tier: a long-lived connection must not leak descriptors.

The daemon holds a LanceDB connection for its whole lifetime and rebuilds the
FTS/scalar index on every sync. Each ``create_fts_index(replace=True)`` supersedes
an index generation and deletes the old files, but LanceDB's Rust core keeps the
readers open — one leaked descriptor per generation — until the process hits
``RLIMIT_NOFILE`` and ``quarry find`` starts returning HTTP 500. A short-lived CLI
never notices; only a long-lived process accumulates.

Two workloads exercise the same invariant. The optimize-loop test stands in for
the daemon's sync: one connection, many ``optimize`` cycles, each an FTS rebuild.
The backfill test stands in for a single ``backfill-sessions`` run: one connection
ingesting hundreds of transcripts back-to-back, each ingest reopening the table
and touching the FTS index. In both, the leaking code path lets open fds ramp
with the workload; a bounded implementation plateaus. The backfill test is the
regression guard that lets ``--limit`` be a pure pagination knob rather than a
magic-number safety belt: it proves the run is bounded by construction, not by a
500-transcript cap.

The soft fd limit is raised to the hard limit for the duration so the leaking
path fails as a clean assertion rather than an ``EMFILE`` crash mid-run.
"""

from __future__ import annotations

import gc
import json
import resource
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, final
from unittest.mock import patch

import numpy as np
import pytest

from quarry.backfill import backfill_sessions, encode_project_path
from quarry.config import Settings
from quarry.db import Database
from quarry.ingestion.file_indexer import SingleFileIndexer
from quarry.models import Chunk
from quarry.sync_finalize import SyncFinalizer
from quarry.sync_registry import SyncRegistry

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


_BACKFILL_TRANSCRIPTS = 250


@final
class _FdSamplingEmbedder:
    """A stand-in embedding backend that samples open fds on every call.

    Backfill embeds each transcript in one bounded window, so ``embed_texts`` is
    invoked once per transcript. Recording the descriptor count here turns the
    embed hook into a per-transcript fd probe without touching the real ONNX
    model, keeping the test hermetic and CI-runnable.
    """

    __slots__ = ("_dim", "_trajectory")

    _dim: int
    _trajectory: FdTrajectory

    def __new__(cls, trajectory: FdTrajectory, *, dimension: int) -> Self:
        self = super().__new__(cls)
        self._trajectory = trajectory
        self._dim = dimension
        return self

    @property
    def dimension(self) -> int:
        """Embedding width the fake vectors are shaped to."""
        return self._dim

    @property
    def model_name(self) -> str:
        """Identify the fake backend in any diagnostic output."""
        return "fd-sampling-fake"

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Sample open fds, then return one random vector per text."""
        self._trajectory.record(_open_fd_count())
        return _random_vectors(len(texts))

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Return a single random query vector (unused by backfill)."""
        vector: NDArray[np.float32] = _random_vectors(1)[0]
        return vector


def _write_transcript(path: Path, index: int) -> None:
    """Write a minimal one-exchange JSONL transcript with unique text."""
    lines = [
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"probe question {index}"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": f"probe answer {index} lorem"}],
            },
        },
    ]
    with path.open("w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _fabricate_backfill_corpus(tmp_path: Path, count: int) -> tuple[Settings, Path]:
    """Build *count* transcripts, a registry, and settings for one backfill run.

    Returns the settings and the fake ``~/.claude/projects`` root the backfill
    scans (patched into the module under test by the caller).
    """
    project = tmp_path / "myproject"
    project.mkdir()
    encoded = encode_project_path(str(project.resolve()))
    project_dir = tmp_path / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    for i in range(count):
        _write_transcript(project_dir / f"{i:08d}-0000-0000-0000-000000000000.jsonl", i)

    registry_path = tmp_path / "registry.db"
    conn = SyncRegistry(registry_path)
    try:
        conn.register_directory(project, "myproject")
    finally:
        conn.close()

    settings = Settings.load().resolve_db_paths(None)
    settings = settings.model_copy(
        update={"lancedb_path": tmp_path / "lancedb", "registry_path": registry_path}
    )
    return settings, tmp_path / ".claude" / "projects"


# One backfill run ingesting hundreds of transcripts is heavier per iteration
# than the optimize loop (file read + scrub + capture write + table reopen), so
# give it its own headroom rather than shrinking the corpus into insignificance.
@pytest.mark.timeout(180)
@pytest.mark.usefixtures("_raised_fd_limit")
def test_large_backfill_does_not_leak_descriptors(tmp_path: Path) -> None:
    """A single-connection backfill of hundreds of transcripts must plateau fds.

    This is the invariant that lets ``--limit`` be a pagination convenience
    rather than a resource-safety belt: if open fds are bounded by construction
    across the whole run, no 500-transcript cap is protecting anything. The fake
    embedder samples fds once per transcript; a leak in the ingest/reopen path
    would ramp the trajectory, a bounded path holds it flat.
    """
    settings, projects_dir = _fabricate_backfill_corpus(tmp_path, _BACKFILL_TRANSCRIPTS)
    trajectory = FdTrajectory()
    embedder = _FdSamplingEmbedder(trajectory, dimension=_EMBEDDING_DIM)

    with (
        patch("quarry.backfill.CLAUDE_PROJECTS_DIR", projects_dir),
        patch(
            "quarry.ingestion.streaming.get_embedding_backend",
            return_value=embedder,
        ),
    ):
        stats = backfill_sessions(settings)

    assert stats.ingested == _BACKFILL_TRANSCRIPTS, (
        f"backfill ingested {stats.ingested} of {_BACKFILL_TRANSCRIPTS} "
        f"transcripts; errors={stats.errors[:3]}"
    )
    assert trajectory.plateaus(slack=_PLATEAU_SLACK), (
        f"open fds grew by {trajectory.growth():.1f} across "
        f"{_BACKFILL_TRANSCRIPTS} backfilled transcripts (peak {trajectory.peak}); "
        f"descriptor leak in the backfill ingest path"
    )


_WATCH_EDITS = 120
_WATCH_DATABASES = 2
_FINALIZE_EVERY = 10
_DISTINCT_FILES = 5


@final
class _ConstantEmbedder:
    """A hermetic embedder returning random vectors of the configured width."""

    __slots__ = ("_dim",)

    _dim: int

    def __new__(cls, *, dimension: int) -> Self:
        self = super().__new__(cls)
        self._dim = dimension
        return self

    @property
    def dimension(self) -> int:
        """Embedding width the fake vectors are shaped to."""
        return self._dim

    @property
    def model_name(self) -> str:
        """Identify the fake backend in diagnostics."""
        return "watch-fd-fake"

    def embed_texts(self, texts: list[str]) -> NDArray[np.float32]:
        """Return one random vector per text."""
        return _random_vectors(len(texts))

    def embed_query(self, query: str) -> NDArray[np.float32]:
        """Return a single random query vector (unused here)."""
        vector: NDArray[np.float32] = _random_vectors(1)[0]
        return vector


def _watch_database(tmp_path: Path, index: int) -> tuple[Database, Settings, Path]:
    """Build one database's persistent connection, settings, and watched root."""
    base = tmp_path / f"db{index}"
    (base / "lancedb").mkdir(parents=True)
    root = tmp_path / f"proj{index}"
    root.mkdir()
    settings = Settings.load().resolve_db_paths(None)
    settings = settings.model_copy(
        update={
            "lancedb_path": base / "lancedb",
            "registry_path": base / "registry.db",
        }
    )
    # The files table FKs the directories row, so the watched root must be
    # registered before any file under it is indexed (as register does in prod).
    conn = SyncRegistry(settings.registry_path)
    try:
        conn.register_directory(root.resolve(), "col")
    finally:
        conn.close()
    return Database.connect(base / "lancedb"), settings, root.resolve()


def _watch_index_one(db: Database, settings: Settings, root: Path, doc: Path) -> None:
    """Index one file through a fresh thread-bound registry, as FileIndexJob does."""
    conn = SyncRegistry(settings.registry_path)
    try:
        SingleFileIndexer(
            db.store, conn, settings, collection="col", resolved=root
        ).index_one(doc)
    finally:
        conn.close()


# Hundreds of small single-file edits across two databases' own connections, with
# the FTS rebuild coalesced to a periodic finalize (not per file) — the DES-045
# §9 shape. Its own headroom, like the sibling resource tests.
@pytest.mark.timeout(180)
@pytest.mark.usefixtures("_raised_fd_limit")
def test_watch_session_does_not_leak_descriptors(tmp_path: Path) -> None:
    """A long watch session over >=2 databases must plateau the aggregate fds.

    This is the proof that per-file indexing (``FileIndexJob`` → ``index_one``)
    with the FTS rebuild coalesced to a post-quiescence ``CollectionFinalizeJob``
    does not reopen quarry-0dss across the daemon's multiple persistent
    connections. If per-file indexing rebuilt the FTS, or the finalize ran per
    file, the trajectory would ramp; the coalesced design holds it flat.
    """
    databases = [_watch_database(tmp_path, i) for i in range(_WATCH_DATABASES)]
    trajectory = FdTrajectory()
    embedder = _ConstantEmbedder(dimension=_EMBEDDING_DIM)

    with patch(
        "quarry.ingestion.streaming.get_embedding_backend", return_value=embedder
    ):
        for edit in range(_WATCH_EDITS):
            db, settings, root = databases[edit % _WATCH_DATABASES]
            doc = root / f"note{edit % _DISTINCT_FILES}.md"
            doc.write_text(f"watch probe {edit} lorem ipsum dolor sit amet consectetur")
            _watch_index_one(db, settings, root, doc)
            # Coalesced FTS rebuild — once per quiescent batch, never per file.
            if edit % _FINALIZE_EVERY == 0:
                SyncFinalizer(db.db, settings).run()
            trajectory.record(_open_fd_count())

    assert trajectory.plateaus(slack=_PLATEAU_SLACK), (
        f"open fds grew by {trajectory.growth():.1f} across {_WATCH_EDITS} watch "
        f"edits over {_WATCH_DATABASES} databases (peak {trajectory.peak}); the "
        f"coalesced FTS rebuild (DES-045 §9) is not holding the descriptor count"
    )
