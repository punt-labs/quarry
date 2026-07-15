"""Request contracts for the optimize and backfill operations (not yet routed)."""

from __future__ import annotations

from pydantic import BaseModel


class OptimizeRequest(BaseModel):
    """Body for the planned ``POST /optimize`` — not on the current wire.

    Compacts the table and rebuilds indexes. ``force`` bypasses the
    fragment-count safety guard (manual recovery).
    """

    force: bool = False


class BackfillRequest(BaseModel):
    """Body for the planned ``POST /backfill-sessions`` — not on the current wire.

    Ingests historical transcripts. ``limit == 0`` means no limit;
    ``collection``/``project`` narrow the scan.
    """

    dry_run: bool = False
    collection: str = ""
    project: str = ""
    limit: int = 0
