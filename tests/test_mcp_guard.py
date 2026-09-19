"""Behaviour of :class:`quarry.mcp_guard.ToolGuard`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.mcp_guard import ToolGuard

if TYPE_CHECKING:
    import pytest


class TestWrap:
    def test_returning_tool_passes_through(self) -> None:
        @ToolGuard.wrap
        def tool(name: str, *, loud: bool = False) -> str:
            return name.upper() if loud else name

        assert tool("ok") == "ok"
        assert tool("ok", loud=True) == "OK"

    def test_raising_tool_returns_error_string_and_logs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @ToolGuard.wrap
        def tool() -> str:
            msg = "daemon said no"
            raise ValueError(msg)

        with caplog.at_level("ERROR", logger="quarry.mcp_guard"):
            result = tool()

        assert result == "Error: ValueError: daemon said no"
        record = next(r for r in caplog.records if r.name == "quarry.mcp_guard")
        assert "Error in tool" in record.getMessage()
        assert record.exc_info is not None  # the traceback travels with the log

    def test_preserves_the_name_fastmcp_registers(self) -> None:
        @ToolGuard.wrap
        def find(query: str) -> str:
            """Search things."""
            return query

        assert find.__name__ == "find"
        assert find.__doc__ == "Search things."

    def test_is_a_stateless_namespace(self) -> None:
        assert ToolGuard.__slots__ == ()
