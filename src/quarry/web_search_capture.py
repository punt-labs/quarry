"""Parse a PostToolUse WebSearch hook payload into a searchable digest.

Mirrors :mod:`quarry.web_capture` for search results: the payload's ``query``
and ``tool_response`` become a short markdown digest that ``handle_post_web_search``
files under ``<repo>-captures``.  A full page fetch is not attempted — WebSearch
never fetches page bodies; the digest is the useful signal.

The exact shape of ``tool_response`` is under-specified in Claude Code's own
docs, so parsing is defensive: any failure yields ``None`` for the property in
question and the handler skips the capture.  Absence is the documented contract,
not a failure — see :class:`WebFetchPayload` for the precedent this file mirrors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebSearchPayload:
    """A PostToolUse WebSearch payload, parsed into what a capture needs.

    ``query`` is the search string the agent submitted; ``digest`` is a short
    markdown summary of the result list (title, url, snippet per result).  Both
    return ``None`` when the payload lacks a usable value.
    """

    _raw: dict[str, object]

    @property
    def query(self) -> str | None:
        """Return the search query string, or ``None`` when absent/blank."""
        tool_input = self._raw.get("tool_input")
        if isinstance(tool_input, dict):
            query = tool_input.get("query")
            if isinstance(query, str) and query.strip():
                return query.strip()
        return None

    @property
    def digest(self) -> str | None:
        """Return a markdown digest of the search results, or ``None``.

        Three shapes for ``tool_response`` are supported, in order of the
        Claude Code payloads observed so far:

        1. A JSON-encoded list/dict of structured result items with
           ``title``/``url``/``snippet`` fields — the original shape.
        2. A dict-shaped payload wrapping the results under ``results``
           or ``result``.
        3. A plain text/markdown string that Claude Code's newer
           WebSearch handler emits directly (post the 2026-05 revision
           the tool response is a rendered summary, not a structured
           list).  Falling back to the raw text keeps the capture
           useful when the extractor would otherwise skip.

        ``None`` still means "nothing to capture" — the tool response
        genuinely has no textual content.
        """
        structured = self._structured_digest()
        if structured is not None:
            return structured
        text = self._text_fallback()
        if not text:
            return None
        return f"# Web search: {self.query or '(no query)'}\n\n{text}"

    def _structured_digest(self) -> str | None:
        """Return the structured-list digest, or ``None`` when no items parse."""
        results = self._results()
        if not results:
            return None
        lines = [f"# Web search: {self.query or '(no query)'}\n"]
        for item in results:
            line = self._format_item(item)
            if line:
                lines.append(line)
        return "\n".join(lines) if len(lines) > 1 else None

    def _text_fallback(self) -> str:
        """Return raw tool_response text, or ``""`` when unusable.

        A string that parses as a JSON container (list/dict) is left to
        the structured path — the fallback only fires when the payload
        is genuine text.  An empty list or invalid JSON container yields
        ``""`` so a content-free capture is skipped, matching the
        pre-G5 contract.  A plain text/markdown string is returned as
        is; an already-parsed dict falls through to the known text keys.
        """
        raw = self._raw.get("tool_response")
        if isinstance(raw, dict):
            return self._dict_text_field(raw)
        if not isinstance(raw, str):
            return ""
        stripped = raw.strip()
        if not stripped:
            return ""
        # If it parses as a container, structured_digest owned the case;
        # a scalar/string parse or a parse failure means it's real text.
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            return stripped
        if isinstance(parsed, (dict, list)):
            return ""
        return stripped

    @classmethod
    def _dict_text_field(cls, parsed: dict[str, object]) -> str:
        """Return the first non-blank string under a known text key, else ``""``."""
        for key in ("content", "text", "summary"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _results(self) -> list[object]:
        """Return the raw result items from ``tool_response``, or an empty list."""
        raw = self._raw.get("tool_response")
        parsed = self._as_parsed(raw)
        if isinstance(parsed, list):
            return list(parsed)
        if isinstance(parsed, dict):
            candidates = parsed.get("results")
            if candidates is None:
                candidates = parsed.get("result")
            if isinstance(candidates, list):
                return list(candidates)
        return []

    @staticmethod
    def _as_parsed(raw: object) -> object:
        """Return *raw* decoded from JSON if it's a string, else *raw* itself."""
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None
        return raw

    @classmethod
    def _format_item(cls, item: object) -> str:
        """Format a single result item as ``- [title](url): snippet``.

        Missing fields degrade individually — a hit with only a URL still emits
        a useful line.  A non-dict item is dropped.
        """
        if not isinstance(item, dict):
            return ""
        title = cls._str_field(item.get("title"))
        url = cls._str_field(item.get("url"))
        snippet = cls._str_field(item.get("snippet") or item.get("description"))
        if not (title or url or snippet):
            return ""
        label = title or url or "result"
        head = f"- [{label}]({url})" if url else f"- {label}"
        return f"{head}: {snippet}" if snippet else head

    @staticmethod
    def _str_field(value: object) -> str:
        """Return *value* stripped when it is a non-blank string, else ``""``."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        return ""
