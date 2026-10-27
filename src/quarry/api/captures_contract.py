"""The captures wire contract: push redacted shadows, look up a URL."""

from __future__ import annotations

from pydantic import BaseModel


class CapturesPushResponse(BaseModel):
    """The response for pushing redacted capture shadows.

    ``results`` maps each pushed collection to its per-collection push summary
    (``CaptureSync`` result dict) — a wire-boundary mapping the CLI renders,
    kept as ``dict`` so a summary-field addition never drops on the wire.
    """

    # wire boundary — per-collection push summaries keyed by collection name.
    results: dict[str, dict[str, object]]


class CapturesLookupRequest(BaseModel):
    """Body for looking up whether a URL is already indexed under captures.

    POST, not a query string: *url* can carry a secret token (an API key in
    the query, a session id in the path) that a GET would otherwise expose to
    proxy, WAF, and browser history logs (CWE-598). Mirrors ``POST /capture``'s
    ``cwd``-derived collection contract.
    """

    url: str
    cwd: str = ""


class CapturesLookupResponse(BaseModel):
    """Whether a URL is already indexed under a project's captures collection.

    ``document_name`` is the URL's canonical name when ``matched`` (``None`` on a
    miss, not a failure).  ``legacy``: stored under an older spelling, never returned.
    """

    matched: bool
    document_name: str | None = None
    legacy: bool = False
