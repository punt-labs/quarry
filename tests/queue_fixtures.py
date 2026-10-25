"""A daemon context with a real ingest queue and task registry, over fake settings."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from quarry.daemon.ingest_queue import IngestQueue
from quarry.daemon.tasks import TaskRegistry

if TYPE_CHECKING:
    from quarry.daemon.context import DaemonContext


def ctx_with_queue(tmp_path: Path, *, depth: int = 32) -> DaemonContext:
    """Return a context with a real queue and registry, over a fake settings."""
    settings = SimpleNamespace(
        ingest_embed_concurrency=1,
        ingest_queue_depth=depth,
        ingest_drain_timeout_s=30.0,
        ingest_max_workers=8,
        ingest_worker_idle_s=60.0,
        quarry_root=Path(tempfile.mkdtemp(dir=tmp_path)),
        registry_path=tmp_path / "registry.db",
    )
    ctx = SimpleNamespace(
        settings=settings, database_name="db", database=object(), tasks=TaskRegistry()
    )
    daemon = cast("DaemonContext", ctx)
    ctx.ingest_queue = IngestQueue(daemon)
    return daemon
