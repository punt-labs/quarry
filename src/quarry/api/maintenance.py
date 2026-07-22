"""Request contracts for the optimize and backfill maintenance operations."""

from __future__ import annotations

from pydantic import BaseModel


class OptimizeRequest(BaseModel):
    """Body for compacting the table and rebuilding indexes.

    ``force`` bypasses the fragment-count safety guard (manual recovery).
    """

    force: bool = False


class BackfillRequest(BaseModel):
    """Body for ingesting historical session transcripts.

    ``limit`` is a pure pagination knob, identical on the local and remote
    paths: ``0`` (the default) means "all" and a positive value caps the scan,
    with no ceiling — a run is bounded by construction (one transcript at a
    time, no descriptor leak).  ``collection``/``project`` narrow the scan.
    """

    dry_run: bool = False
    collection: str = ""
    project: str = ""
    limit: int = 0
