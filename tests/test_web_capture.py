"""Tests for quarry.web_capture — WebFetch payload parsing."""

from __future__ import annotations

import json

from quarry.web_capture import WebFetchPayload


class TestUrl:
    def test_extracts_url_from_tool_input(self) -> None:
        payload: dict[str, object] = {"tool_input": {"url": "https://example.com/docs"}}
        assert WebFetchPayload(payload).url == "https://example.com/docs"

    def test_returns_none_for_missing_tool_input(self) -> None:
        assert WebFetchPayload({}).url is None

    def test_returns_none_for_non_dict_tool_input(self) -> None:
        assert WebFetchPayload({"tool_input": "not a dict"}).url is None

    def test_returns_none_for_non_http_url(self) -> None:
        payload: dict[str, object] = {"tool_input": {"url": "ftp://x.com"}}
        assert WebFetchPayload(payload).url is None

    def test_returns_none_for_missing_url(self) -> None:
        payload: dict[str, object] = {"tool_input": {"other": "value"}}
        assert WebFetchPayload(payload).url is None


class TestContent:
    def test_extracts_from_json_result_field(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps({"result": "<html>Hello</html>"}),
        }
        assert WebFetchPayload(payload).content == "<html>Hello</html>"

    def test_extracts_from_json_string(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps("Plain text content"),
        }
        assert WebFetchPayload(payload).content == "Plain text content"

    def test_returns_none_for_missing_tool_response(self) -> None:
        assert WebFetchPayload({}).content is None

    def test_returns_none_for_non_string_tool_response(self) -> None:
        assert WebFetchPayload({"tool_response": 42}).content is None

    def test_returns_none_for_invalid_json(self) -> None:
        assert WebFetchPayload({"tool_response": "not json{{"}).content is None

    def test_returns_none_for_empty_result(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps({"result": "  "}),
        }
        assert WebFetchPayload(payload).content is None

    def test_returns_none_for_empty_string(self) -> None:
        payload: dict[str, object] = {
            "tool_response": json.dumps("   "),
        }
        assert WebFetchPayload(payload).content is None
