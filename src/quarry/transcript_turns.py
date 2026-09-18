"""The text one Claude Code transcript record carries: a user or assistant turn."""

from __future__ import annotations

from typing import Final, final

_TURN_TYPES: Final = ("user", "assistant")


@final
class TurnText:
    """Extract the conversation text from a single transcript record.

    Stateless — every method derives from the record alone — so the reader
    that walks a whole file and the report that wants one final turn extract
    text the same way. Tool-use blocks are not text; a tool result is kept
    only when it is short enough to read as part of the conversation.
    """

    __slots__ = ()

    _MAX_TOOL_RESULT_CHARS = 500

    @classmethod
    def message_text(cls, record: dict[str, object]) -> str | None:  # None: not a turn
        """Return a single record's ``[role] text``, or ``None`` if not a message.

        ``None`` is the documented "this record is not a user/assistant message"
        contract (a file snapshot or system record), not a failure — the caller
        skips it.
        """
        text = cls.turn_text(record)
        if not text:
            return None
        return f"[{cls._role(record)}] {text}"

    @classmethod
    def turn_text(cls, record: dict[str, object]) -> str:
        """Return a user/assistant record's text with no role prefix, or ``""``.

        A string ``content`` is returned as written (blank-only is ``""``); a
        block list yields its text blocks and short tool results joined by a
        space.
        """
        if record.get("type") not in _TURN_TYPES:
            return ""
        message = record.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content if content.strip() else ""
        if not isinstance(content, list):
            return ""
        return " ".join(cls._content_texts(content))

    @staticmethod
    def _role(record: dict[str, object]) -> str:
        """Return the message's ``role``, falling back to the record ``type``."""
        message = record.get("message")
        role = message.get("role") if isinstance(message, dict) else None
        return str(role) if isinstance(role, str) else str(record.get("type", ""))

    @classmethod
    def _content_texts(cls, content: list[object]) -> list[str]:
        """Extract text fragments from a list of content blocks."""
        texts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                if tool_text := cls._tool_result_text(block):
                    texts.append(f"[tool_result] {tool_text}")
            elif stripped := cls._block_text(block):
                texts.append(stripped)
        return texts

    @classmethod
    def _tool_result_text(cls, block: dict[str, object]) -> str:
        """Return the tool_result text if under the per-result cap, else ``""``."""
        tool_content = block.get("content")
        if isinstance(tool_content, str):
            tool_text = tool_content.strip()
        elif isinstance(tool_content, list):
            tool_text = " ".join(
                t
                for b in tool_content
                if isinstance(b, dict) and (t := cls._block_text(b))
            )
        else:
            tool_text = ""
        if 0 < len(tool_text) <= cls._MAX_TOOL_RESULT_CHARS:
            return tool_text
        return ""

    @staticmethod
    def _block_text(block: dict[str, object]) -> str:
        """Return a text block's stripped text, or ``""`` if not a text block."""
        if block.get("type") != "text":
            return ""
        text = block.get("text")
        return text.strip() if isinstance(text, str) else ""
