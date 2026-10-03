"""Behaviour of :class:`quarry.mcp_documents.DocumentTools`.

The full daemon-backed exercise of each tool (register/delete/show against a
real ``/v1`` handler) lives in ``tests/test_mcp_server.py``'s ``harness``
fixture, which this module's classes already use via ``harness.documents``.
This file covers what that harness cannot: registration under the server, the
guard boundary with no daemon at all, and input validation that short-circuits
before a connection is ever opened.
"""

from __future__ import annotations

import asyncio

from mcp.server.mcpserver import MCPServer

from quarry.client import QuarryClient, QuarryConnectionError
from quarry.mcp_documents import DocumentTools


def _down() -> QuarryClient:
    raise QuarryConnectionError("quarryd is not running", "[IP_ADDRESS]")


def _refuse() -> QuarryClient:
    raise AssertionError("guard must short-circuit before connecting")


class TestRegister:
    def test_registers_every_lifecycle_tool(self) -> None:
        server = MCPServer("t")
        DocumentTools(connect=_down).register(server)
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        assert names == {
            "show",
            "delete",
            "register_directory",
            "deregister_directory",
            "sync_all_registrations",
        }

    def test_each_tool_has_an_occasion_opener(self) -> None:
        openers = {
            "show": "Use to read a specific page",
            "delete": "Use to remove stale or wrong content",
            "register_directory": "Use to track a local directory",
            "deregister_directory": "Use to stop tracking a directory",
            "sync_all_registrations": "Use after registering a new directory",
        }
        for name, opener in openers.items():
            doc = getattr(DocumentTools, name).__doc__
            assert doc is not None, name
            assert doc.lstrip().startswith(opener), name


class TestBoundary:
    def test_daemon_down_returns_an_error_string(self) -> None:
        result = DocumentTools(connect=_down).show("report.pdf")
        assert result.startswith("Error:")
        assert "not running" in result

    def test_direct_and_registered_calls_share_the_boundary(self) -> None:
        server = MCPServer("t")
        tools = DocumentTools(connect=_down)
        tools.register(server)
        direct = tools.sync_all_registrations()
        registered = asyncio.run(server.call_tool("sync_all_registrations", {}))
        assert direct in str(registered)


class TestInputValidation:
    """Blank required args are caller errors — they short-circuit before
    ``_connect``, so a client that raises on connect proves the guard fired
    first.
    """

    def test_delete_blank_name(self) -> None:
        result = DocumentTools(connect=_refuse).delete("")
        assert result.startswith("Error:")
        assert "name" in result

    def test_register_blank_directory(self) -> None:
        result = DocumentTools(connect=_refuse).register_directory("   ")
        assert result.startswith("Error:")
        assert "directory" in result

    def test_deregister_blank_collection(self) -> None:
        result = DocumentTools(connect=_refuse).deregister_directory("")
        assert result.startswith("Error:")
        assert "collection" in result

    def test_show_blank_document_name(self) -> None:
        result = DocumentTools(connect=_refuse).show("   ")
        assert result.startswith("Error:")
        assert "document_name" in result
