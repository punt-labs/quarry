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
    ``scrub`` marks a web-fetch capture re-fetch: the daemon scrubs the fetched
    page and stores it in the ``<repo>-captures`` collection derived from
    ``cwd`` (``default-captures`` when ``cwd`` is empty/unregistered). Without
    ``scrub`` this is a plain ``quarry ingest`` — sitemap-aware and unscrubbed.
    """

    source: str
    overwrite: bool = False
    collection: str = ""
    cwd: str = ""
    scrub: bool = False
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""
