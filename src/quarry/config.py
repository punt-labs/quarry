"""Application settings: LanceDB paths, embedding model, and chunking."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar, Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

ONNX_MODEL_REPO = "Snowflake/snowflake-arctic-embed-m-v1.5"
ONNX_MODEL_REVISION = "e58a8f756156a1293d763f17e3aae643474e9b8a"
ONNX_TOKENIZER_FILE = "tokenizer.json"
ONNX_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# The resident daemon's file-descriptor budget: the soft RLIMIT_NOFILE FdEnvelope
# raises to at daemon start (DES-046). ~25x the bounded working set of a ~21-db
# roster, well under any reasonable hard limit — years of roster growth headroom.
DEFAULT_FD_LIMIT = 8192

# The data root when QUARRY_ROOT is unset.  One binding shared by the field
# default and by data_root(), so the two can never disagree about where a
# default deployment lives.
_DEFAULT_QUARRY_ROOT: Final[Path] = Path.home() / ".punt-labs" / "quarry" / "data"


class Settings(BaseSettings):
    quarry_root: Path = _DEFAULT_QUARRY_ROOT
    lancedb_path: Path = quarry_root / "default" / "lancedb"
    registry_path: Path = quarry_root / "default" / "registry.db"
    embedding_model: str = "Snowflake/snowflake-arctic-embed-m-v1.5"
    embedding_dimension: int = 768

    chunk_max_chars: int = 1800
    chunk_overlap_chars: int = 200

    # Bounded progressive commit (DES-034); embed_window_chunks is a kpz seam.
    # Both are >= 1: ProgressiveIndexer rejects a non-positive flush budget, so an
    # invalid value must fail loud at construction, not deep in the ingestor.
    sync_flush_mb: int = Field(default=32, ge=1)
    embed_window_chunks: int = Field(default=512, ge=1)

    # Serialized capture/index queue (DES-042).  embed_concurrency is clamped to
    # the queue's hard ceiling regardless of this value; queue_depth bounds the
    # admitted (in-flight + waiting) jobs; drain_timeout_s bounds the shutdown
    # drain.  The collection key is client-controlled, so max_workers caps
    # resident workers and worker_idle_s reaps idle ones.
    ingest_embed_concurrency: int = Field(default=1, ge=1)
    ingest_queue_depth: int = Field(default=32, ge=1)
    ingest_drain_timeout_s: float = Field(default=30.0, ge=0)
    ingest_max_workers: int = Field(default=256, ge=1)
    ingest_worker_idle_s: float = Field(default=60.0, ge=0)

    # Always-on filesystem watch loop, a DES-042 queue producer across every
    # database in the roster (DES-045).  ``debounce_s`` coalesces an edit burst;
    # ``max_delay_s`` caps a continuously-rearmed path (anti-starvation); a delta
    # above ``bulk_threshold`` distinct paths collapses to one bulk scan (fragment
    # budget + admission bound); ``use_polling`` forces watchdog's stat-walk
    # observer; ``safety_scan_s`` is the periodic roster reconcile that re-scans
    # collections whose scan was shed and picks up databases/collections
    # registered since start — the backstop that retires the uae timer.
    # 0 disables the periodic task entirely (watch_loop.py only creates it when
    # > 0) — this is a TEST-ONLY convention throughout this suite (drive
    # _reconcile directly instead of waiting on a timer) and must never be set
    # in production: it removes the ONLY backstop for a missed/shed live-watch
    # event, a kernel inotify queue overflow (silently dropped by the OS, not
    # recoverable any other way), or a watch that degraded to scan-only.
    watch_enabled: bool = True
    watch_debounce_s: float = Field(default=1.0, ge=0)
    watch_max_delay_s: float = Field(default=5.0, ge=0)
    watch_bulk_threshold: int = Field(default=50, ge=1)
    watch_use_polling: bool = False
    watch_poll_interval_s: float = Field(default=2.0, gt=0)
    watch_safety_scan_s: float = Field(default=300.0, ge=0)
    # Rate-limit the finalize (optimize + full FTS rebuild) the daemon runs behind
    # each indexed batch, so ordinary edit activity under the registered repos does
    # not drive a heavy compaction every debounce window.  A finalize runs at most
    # once per interval per database; batches inside the window coalesce into one
    # trailing finalize when it elapses (FTS lags, vector channel stays fresh;
    # DES-045 §9).  0 disables the rate-limit (finalize every batch).
    watch_optimize_min_interval_s: float = Field(default=30.0, ge=0)

    # Soft RLIMIT_NOFILE the daemon raises to at start (DES-046), overridable via
    # QUARRY_FD_LIMIT.  Coerced fail-safe: a malformed or non-positive env value
    # degrades to the default with one logged warning rather than crashing the
    # daemon at construction (Bug-class-2) — the whole point of the raise is to
    # survive, so a typo in the override must not defeat it.
    fd_limit: int = Field(default=DEFAULT_FD_LIMIT, validation_alias="QUARRY_FD_LIMIT")

    # Agent-memory temporal decay rate (1/hour) threaded into RetrievalConfig
    # at wire time (see daemon/routes/search.py).  The default is a 30-day
    # half-life: ln(2) / 720h ≈ 0.000963.  RRF rank-1 weight is 1/(60+0) ≈
    # 0.0167; a 30-day-old memory decays to weight 0.5, roughly the difference
    # between ranks 1 and 60 — a month-old top hit loses to a fresh rank-2.
    # 7 days would decay useful memories before they mature; 90 days ≈ no
    # decay at typical session cadences.  Set to 0.0 to disable decay; a
    # negative rate would invert the curve (older rows outrank newer), so
    # ``ge=0`` fails loud at construction.
    retrieval_decay_rate: float = Field(
        default=0.000963,
        ge=0,
        validation_alias="QUARRY_RETRIEVAL_DECAY_RATE",
    )

    # ``quarry learn``'s retrieval-preference knob, threaded into
    # RetrievalConfig at wire time (see daemon/routes/search.py). RRF's
    # rank-1 term is 1/(60+0); boosting by 1.5x lifts a lesson ranked in the
    # top ~30 of its own channel above even the single best non-lesson hit,
    # while a genuinely poor-relevance lesson (rank 40+) still loses to a
    # relevant plain result -- conservative enough that irrelevant lessons
    # never dominate. ``ge=1.0`` fails loud on a value that would *suppress*
    # lessons instead of boosting them -- 1.0 is how an operator disables the
    # effect, not 0.0, because 0.0 would zero out the row's entire RRF term.
    retrieval_lesson_boost: float = Field(
        default=1.5,
        ge=1.0,
        validation_alias="QUARRY_RETRIEVAL_LESSON_BOOST",
    )

    @field_validator("fd_limit", mode="before")
    @classmethod
    def _coerce_fd_limit(cls, value: object) -> int:
        """Return QUARRY_FD_LIMIT as a positive int, else degrade to the default."""
        try:
            parsed = int(str(value))  # env yields str; the default arrives as int
        except (TypeError, ValueError):
            logger.warning(
                "QUARRY_FD_LIMIT=%r is not an integer; using %d",
                value,
                DEFAULT_FD_LIMIT,
            )
            return DEFAULT_FD_LIMIT
        if parsed < 1:
            logger.warning(
                "QUARRY_FD_LIMIT=%r is not positive; using %d", value, DEFAULT_FD_LIMIT
            )
            return DEFAULT_FD_LIMIT
        return parsed

    model_config = {"env_file": ".env", "extra": "ignore"}

    _DEFAULT_LANCEDB: ClassVar[Path] = quarry_root / "default" / "lancedb"

    def resolve_db_paths(self, db_name: str | None = None) -> Settings:
        """Return a copy with lancedb_path and registry_path resolved.

        With *db_name*, paths resolve under ``quarry_root / db_name``. An explicit
        ``LANCEDB_PATH`` override is preserved; otherwise the ``default`` database
        is used. Raises ``ValueError`` if *db_name* contains path separators or
        traversal segments.
        """
        self._reject_traversal(db_name)

        if self.lancedb_path != Settings._DEFAULT_LANCEDB:
            return self

        name = db_name or "default"
        return self.model_copy(
            update={
                "lancedb_path": self.quarry_root / name / "lancedb",
                "registry_path": self.quarry_root / name / "registry.db",
            },
        )

    @staticmethod
    def _reject_traversal(db_name: str | None) -> None:
        """Raise when *db_name* could escape ``quarry_root``.

        A database name becomes a path segment, so a separator or a dot segment
        would place the database outside the root entirely. Validated at this
        boundary and trusted below it.
        """
        if db_name is not None and (
            "/" in db_name or "\\" in db_name or db_name in (".", "..")
        ):
            msg = f"Invalid database name: {db_name!r}"
            raise ValueError(msg)

    @classmethod
    def data_root(cls) -> Path:
        """Return the configured data root, resolved per call.

        Resolved through the model rather than by reading ``QUARRY_ROOT`` out of
        the environment, so that every source pydantic-settings honours decides
        the root exactly once. A direct environment read agrees with the field
        only for the process-environment case: ``env_file`` means a ``.env``
        entry sets the field and *not* ``os.environ``, and the two then disagree
        — the data would relocate while anything derived from the direct read
        stayed behind, next to the operator's real tree.

        Costs 273 us against 0.4 us for the raw environment lookup (measured,
        2000 iterations). Resolution happens a handful of times per process, so
        well under a millisecond in total; a single source of truth is worth
        that, and the alternative is a divergence that writes to a production
        path.
        """
        return cls().quarry_root

    @classmethod
    def load(cls) -> Settings:
        """Load application settings. Fresh instance each call."""
        return cls()


DEFAULT_PORT = 8420  # well-known port for ``quarryd`` + mcp-proxy configs
