"""Parse a PostToolUse WebFetch hook payload into its URL and fetched content."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebFetchPayload:
    """A PostToolUse WebFetch payload, parsed into the fields a capture needs.

    ``url`` is the fetched location; ``content`` is the already-fetched body the
    hook received, whose reuse avoids a second network request — and the extra
    SSRF exposure of quarry issuing its own fetch.  Both are ``None`` when the
    payload lacks a usable value; absence is the documented contract.
    """

    _raw: dict[str, object]

    @property
    def url(self) -> str | None:
        """Return the fetched HTTP(S) URL, or ``None`` if absent or non-HTTP."""
        tool_input = self._raw.get("tool_input")
        if isinstance(tool_input, dict):
            url = tool_input.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
        return None

    @property
    def content(self) -> str | None:
        """Return the already-fetched body from ``tool_response``, or ``None``.

        ``tool_response`` is a JSON-encoded string holding the fetched HTML or
        text.  Reusing it keeps quarry off the network on the primary path.
        """
        tool_response = self._raw.get("tool_response")
        if not isinstance(tool_response, str):
            return None
        try:
            parsed = json.loads(tool_response)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict):
            result = parsed.get("result")
            if isinstance(result, str) and result.strip():
                return result
        if isinstance(parsed, str) and parsed.strip():
            return parsed
        return None
