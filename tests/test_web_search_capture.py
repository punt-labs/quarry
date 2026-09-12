"""Behaviour of :class:`quarry.web_search_capture.WebSearchPayload`.

Mirrors ``tests/test_web_capture.py``: valid payloads produce useful fields,
malformed or absent fields yield ``None`` (documented contract).
"""

from __future__ import annotations

import json

from quarry.web_search_capture import WebSearchPayload


class TestQuery:
    def test_extracts_query_from_tool_input(self) -> None:
        payload = WebSearchPayload(
            {"tool_input": {"query": "python 3.13 free-threading"}}
        )
        assert payload.query == "python 3.13 free-threading"

    def test_returns_none_for_blank_query(self) -> None:
        assert WebSearchPayload({"tool_input": {"query": "   "}}).query is None

    def test_returns_none_for_missing_tool_input(self) -> None:
        assert WebSearchPayload({}).query is None

    def test_returns_none_for_non_dict_tool_input(self) -> None:
        assert WebSearchPayload({"tool_input": "not a dict"}).query is None

    def test_returns_none_for_non_string_query(self) -> None:
        assert WebSearchPayload({"tool_input": {"query": 42}}).query is None


class TestDigest:
    def test_extracts_digest_from_json_string(self) -> None:
        results = [
            {
                "title": "PEP 703",
                "url": "https://peps.python.org/pep-0703/",
                "snippet": "Making the GIL optional",
            },
            {
                "title": "Free-threading HOWTO",
                "url": "https://docs.python.org/3.13/howto/free-threading-python.html",
                "snippet": "How to run without the GIL",
            },
        ]
        payload = WebSearchPayload(
            {
                "tool_input": {"query": "free-threading"},
                "tool_response": json.dumps(results),
            }
        )
        digest = payload.digest
        assert digest is not None
        assert digest.startswith("# Web search: free-threading")
        assert "PEP 703" in digest
        assert "Free-threading HOWTO" in digest
        assert "https://peps.python.org/pep-0703/" in digest

    def test_extracts_digest_from_dict_wrapper(self) -> None:
        payload = WebSearchPayload(
            {
                "tool_input": {"query": "onnx runtime"},
                "tool_response": json.dumps(
                    {
                        "results": [
                            {
                                "title": "ONNX",
                                "url": "https://onnx.ai",
                                "snippet": "docs",
                            }
                        ]
                    }
                ),
            }
        )
        digest = payload.digest
        assert digest is not None
        assert "ONNX" in digest

    def test_returns_none_for_empty_results(self) -> None:
        payload = WebSearchPayload(
            {"tool_input": {"query": "x"}, "tool_response": json.dumps([])}
        )
        assert payload.digest is None

    def test_falls_back_to_raw_text_for_non_json_response(self) -> None:
        """A plain string tool_response is the newer Claude Code shape — use it.

        The old contract returned ``None`` on any JSON parse failure, but
        Claude Code's post-2026-05 WebSearch handler emits a rendered
        markdown summary directly rather than a JSON list.  A silent
        skip on that shape is the G5 bug — treat unparseable strings as
        text so the capture still lands.
        """
        payload = WebSearchPayload(
            {
                "tool_input": {"query": "x"},
                "tool_response": "The rendered search summary.",
            }
        )
        digest = payload.digest
        assert digest is not None
        assert "rendered search summary" in digest
        assert "Web search: x" in digest

    def test_returns_none_for_non_string_response(self) -> None:
        payload = WebSearchPayload({"tool_input": {"query": "x"}, "tool_response": 42})
        assert payload.digest is None

    def test_text_fallback_declines_string_json_container(self) -> None:
        """A JSON string that decodes to a container is left to the structured
        path.

        The isinstance check inside ``_text_fallback`` must accept both a
        JSON list AND a JSON dict, and it must not raise on either.  The
        two-arg tuple form is the only shape guaranteed to work under
        ``isinstance`` for every Python; the PEP 604 union form is a
        recurring copy-paste hazard.
        """
        # JSON that decodes to a list — the structured path owns this
        payload = WebSearchPayload(
            {"tool_input": {"query": "x"}, "tool_response": "[]"}
        )
        assert payload.digest is None
        # JSON that decodes to a dict WITHOUT ``results``/``result`` keys
        payload = WebSearchPayload(
            {
                "tool_input": {"query": "x"},
                "tool_response": '{"unrelated": "value"}',
            }
        )
        assert payload.digest is None

    def test_skips_non_dict_items(self) -> None:
        payload = WebSearchPayload(
            {
                "tool_input": {"query": "x"},
                "tool_response": json.dumps(
                    ["a bare string", {"title": "Real", "url": "https://a.example"}]
                ),
            }
        )
        digest = payload.digest
        assert digest is not None
        assert "Real" in digest
        assert "a bare string" not in digest

    def test_digest_uses_url_when_title_missing(self) -> None:
        payload = WebSearchPayload(
            {
                "tool_input": {"query": "x"},
                "tool_response": json.dumps(
                    [{"url": "https://a.example", "snippet": "hi"}]
                ),
            }
        )
        digest = payload.digest
        assert digest is not None
        line = next(line for line in digest.splitlines() if line.startswith("- ["))
        assert line == "- [https://a.example](https://a.example): hi"
