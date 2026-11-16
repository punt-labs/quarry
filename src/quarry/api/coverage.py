"""Wire shape of ``/coverage``: per-repo counts for a collection."""

from __future__ import annotations

from pydantic import BaseModel


class CoverageResponse(BaseModel):
    """Per-repo coverage of a collection and its ``<collection>-captures`` sibling.

    Three distinct-name counts scoped to one repo's slice of the shared catalog:
    non-memory documents in ``collection``, ``session-<id>`` compaction archives
    in ``captures_collection``, and memory rows (``agent_handle``/``memory_type``
    set) in ``collection``. A URL capture in the captures collection is neither
    — it's a background artifact and not surfaced here.
    """

    documents_indexed: int
    transcripts_captured: int
    memories_saved: int
