"""The options of one backfill request, validated and bundled."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BackfillArgs:
    """The validated backfill options, bundled as one value."""

    dry_run: bool
    collection: str
    project: str
    limit: int
