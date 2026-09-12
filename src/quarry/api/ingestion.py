"""Request contracts for the two content-ingestion operations."""

from __future__ import annotations

from pydantic import BaseModel


class RememberRequest(BaseModel):
    """Body for indexing inline text content.

    ``collection`` is the empty-string sentinel: the daemon decides the effective
    collection at ``_remember_job``. Explicit values pass through unchanged; an
    empty value with an ``agent_handle`` routes to ``memory-<handle>``, and an
    empty value with no handle falls back to ``default``.
    """

    name: str
    content: str
    collection: str = ""
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


class LearnRequest(BaseModel):
    """Body for saving a distilled lesson with retrieval preference.

    ``cwd`` is never a caller-facing parameter on any surface (CLI, MCP,
    slash, client) -- ``QuarryClient.learn()`` resolves it via ``Path.cwd()``
    before this model is built, exactly the way ``CaptureIngestRequest.cwd``
    is populated by the caller's own working directory, not the user.
    """

    lesson: str
    topic: str = ""
    name: str = ""
    cwd: str = ""
