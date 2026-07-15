"""Request contracts for the optimize and backfill maintenance operations."""

from __future__ import annotations

from pydantic import BaseModel


class OptimizeRequest(BaseModel):
    """Body for ``POST /v1/optimize`` — compact the table and rebuild indexes.

    ``force`` bypasses the fragment-count safety guard (manual recovery).
    """

    force: bool = False


class BackfillRequest(BaseModel):
    """Body for ``POST /v1/backfill-sessions`` — ingest historical transcripts.

    ``limit == 0`` means no limit; ``collection``/``project`` narrow the scan.
    """

    dry_run: bool = False
    collection: str = ""
    project: str = ""
    limit: int = 0
