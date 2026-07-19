"""The capture-ingest contract: file a scrubbed transcript or fetched page."""

from __future__ import annotations

from pydantic import BaseModel


class CaptureIngestRequest(BaseModel):
    """Body for filing a scrubbed capture: a transcript or a fetched page.

    The daemon derives the target ``<repo>-captures`` collection from ``cwd``
    (falling back to ``default-captures``) and always scrubs before storing.
    The document name is ``document_name`` when set (a fetched URL) or, failing
    that, ``session-<session_id[:8]>`` (a compaction transcript).
    """

    content: str
    cwd: str = ""
    document_name: str = ""
    session_id: str = ""
    overwrite: bool = True
    format_hint: str = "auto"
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""
