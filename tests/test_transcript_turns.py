"""Behaviour of :class:`quarry.transcript_turns.TurnText` — one record's text."""

from __future__ import annotations

from quarry.transcript_turns import TurnText


class TestMessageText:
    def test_extracts_short_tool_result_string(self) -> None:
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": "5 passed, 0 failed",
                    }
                ],
            },
        }
        result = TurnText.message_text(record)
        assert result is not None
        assert "[tool_result] 5 passed, 0 failed" in result

    def test_extracts_short_tool_result_list(self) -> None:
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": [{"type": "text", "text": "No matches found"}],
                    }
                ],
            },
        }
        result = TurnText.message_text(record)
        assert result is not None
        assert "[tool_result] No matches found" in result

    def test_skips_long_tool_result(self) -> None:
        long_output = "x" * 501
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc",
                        "content": long_output,
                    }
                ],
            },
        }
        assert TurnText.message_text(record) is None

    def test_skips_tool_use_blocks(self) -> None:
        record: dict[str, object] = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_abc",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
        }
        assert TurnText.message_text(record) is None

    def test_returns_none_for_non_message_record(self) -> None:
        assert TurnText.message_text({"type": "file-history-snapshot"}) is None

    def test_falls_back_to_type_for_role(self) -> None:
        record: dict[str, object] = {"type": "user", "message": {"content": "hi"}}
        assert TurnText.message_text(record) == "[user] hi"


class TestTurnText:
    def test_string_content_is_returned_verbatim(self) -> None:
        record: dict[str, object] = {
            "type": "assistant",
            "message": {"role": "assistant", "content": "  spaced  "},
        }
        assert TurnText.turn_text(record) == "  spaced  "

    def test_blank_string_content_is_empty(self) -> None:
        record: dict[str, object] = {
            "type": "assistant",
            "message": {"role": "assistant", "content": "   "},
        }
        assert TurnText.turn_text(record) == ""

    def test_non_turn_record_is_empty(self) -> None:
        assert TurnText.turn_text({"type": "system", "message": {}}) == ""

    def test_non_dict_message_is_empty(self) -> None:
        assert TurnText.turn_text({"type": "user", "message": "raw"}) == ""

    def test_non_dict_blocks_are_skipped(self) -> None:
        record: dict[str, object] = {
            "type": "user",
            "message": {
                "role": "user",
                "content": ["loose", {"type": "text", "text": "kept"}],
            },
        }
        assert TurnText.turn_text(record) == "kept"
