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

    Over the daemon, ``limit`` is clamped into ``1..500``: a missing or
    non-positive value and any value above the cap both resolve to 500, so a
    remote request can never trigger an unbounded scan.  (The local CLI still
    treats ``0`` as "all".)  ``collection``/``project`` narrow the scan.
    """

    dry_run: bool = False
    collection: str = ""
    project: str = ""
    limit: int = 0
