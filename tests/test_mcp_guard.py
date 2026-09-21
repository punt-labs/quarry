"""Behaviour of :class:`quarry.mcp_guard.ToolGuard`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quarry.client import QuarryConnectionError
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


class TestConnectionErrorRecovery:
    """A down/unreachable daemon renders a recovery hint, not a bare exception."""

    def test_loopback_target_gets_the_restart_hint(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        @ToolGuard.wrap
        def tool() -> str:
            raise QuarryConnectionError(
                "quarryd is not running", "http://127.0.0.1:8420"
            )

        with caplog.at_level("ERROR", logger="quarry.mcp_guard"):
            result = tool()

        assert result.startswith("Error: quarryd is not running")
        assert "systemctl --user restart quarry" in result
        assert "launchctl kickstart" in result
        assert "QUARRY_URL" not in result  # the remote hint, not this one

    def test_remote_target_gets_the_remote_hint_not_the_restart_hint(self) -> None:
        @ToolGuard.wrap
        def tool() -> str:
            raise QuarryConnectionError(
                "quarryd is not running", "https://quarry.example.com"
            )

        result = tool()

        assert result.startswith("Error: quarryd is not running")
        assert "QUARRY_URL" in result
        assert "systemctl" not in result
        assert "launchctl" not in result

    def test_recovery_message_is_a_single_line(self) -> None:
        """A two-line message would break the direct/registered string-identity
        boundary once FastMCP repr's the result (see test_mcp_missions.py)."""

        @ToolGuard.wrap
        def tool() -> str:
            raise QuarryConnectionError("down", "127.0.0.1")

        assert "\n" not in tool()

    def test_remote_bare_host_with_no_scheme_gets_the_remote_hint(self) -> None:
        """``urlparse`` on a schemeless host yields no ``.hostname``, falling
        back to the raw target -- confirm that fallback still classifies a
        remote bare host as remote, not loopback."""

        @ToolGuard.wrap
        def tool() -> str:
            raise QuarryConnectionError("down", "quarry.example.com")

        result = tool()

        assert "QUARRY_URL" in result
        assert "systemctl" not in result
        assert "launchctl" not in result

    def test_non_connection_error_keeps_the_generic_form(self) -> None:
        @ToolGuard.wrap
        def tool() -> str:
            msg = "not found"
            raise LookupError(msg)

        assert tool() == "Error: LookupError: not found"
