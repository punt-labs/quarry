"""Request contracts for the two content-ingestion operations."""

from __future__ import annotations

from pydantic import BaseModel


class RememberRequest(BaseModel):
    """Body for indexing inline text content."""

    name: str
    content: str
    collection: str = "default"
    format_hint: str = "auto"
    overwrite: bool = True
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""


class IngestRequest(BaseModel):
    """Body for fetching and indexing a URL.

    ``source`` is an ``http(s)`` URL; the daemon validates it against the
    SSRF allowlist before fetching. File upload is not part of this contract.
    ``cwd`` (when set) marks a capture re-fetch: the daemon derives the
    ``<repo>-captures`` collection from it and scrubs the fetched page.
    """

    source: str
    overwrite: bool = False
    collection: str = ""
    cwd: str = ""
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""
