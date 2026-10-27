"""Behaviour of :class:`quarry.mcp_catalog.ResourceCatalog`."""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

from quarry.client import QuarryClient, QuarryConnectionError
from quarry.mcp_catalog import ResourceCatalog


def _down() -> QuarryClient:
    raise QuarryConnectionError("quarryd is not running", "[IP_ADDRESS]")


class TestRegister:
    def test_registers_list_under_its_short_wire_name(self) -> None:
        server = MCPServer("t")
        ResourceCatalog(connect=_down).register(server)
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert names == {"list"}

    def test_tool_has_an_occasion_opener(self) -> None:
        doc = ResourceCatalog.list_resources.__doc__ or ""
        assert doc.lstrip().startswith(
            "Use to see what's already indexed before ingesting"
        )


class TestBoundary:
    def test_daemon_down_returns_an_error_string(self) -> None:
        result = ResourceCatalog(connect=_down).list_resources("documents")
        assert result.startswith("Error:")
        assert "not running" in result

    def test_direct_and_registered_calls_share_the_boundary(self) -> None:
        server = MCPServer("t")
        catalog = ResourceCatalog(connect=_down)
        catalog.register(server)
        direct = catalog.list_resources("documents")
        registered = asyncio.run(server.call_tool("list", {"kind": "documents"}))
        assert direct in str(registered)


class TestUnknownKind:
    def test_unknown_kind_short_circuits_before_connecting(self) -> None:
        def _refuse() -> QuarryClient:
            raise AssertionError("guard must short-circuit before connecting")

        result = ResourceCatalog(connect=_refuse).list_resources("bogus")
        assert "unknown kind" in result
