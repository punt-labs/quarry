"""Request contracts for the two content-ingestion operations."""

from __future__ import annotations

from pydantic import BaseModel


class RememberRequest(BaseModel):
    """Body for ``POST /v1/remember`` — index inline text content."""

    name: str
    content: str
    collection: str = "default"
    format_hint: str = "auto"
    overwrite: bool = True
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""


class IngestRequest(BaseModel):
    """Body for ``POST /v1/ingest`` — fetch and index a URL.

    ``source`` is an ``http(s)`` URL; the daemon validates it against the
    SSRF allowlist before fetching. File upload is not part of this contract.
    """

    source: str
    overwrite: bool = False
    collection: str = ""
    agent_handle: str = ""
    memory_type: str = ""
    summary: str = ""
