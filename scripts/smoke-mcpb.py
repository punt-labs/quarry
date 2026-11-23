"""Verify a published Quarry bundle launches the installed MCP client."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from zipfile import ZipFile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent
from packaging.version import Version


async def smoke_bundle(path: str, version: str, canary: str) -> None:
    with ZipFile(path) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
    if Version(manifest["version"]) != Version(version):
        raise SystemExit(f"Expected MCPB version {version}, got {manifest['version']}")

    config = manifest["server"]["mcp_config"]
    if config != {"command": "quarry", "args": ["mcp"]}:
        raise SystemExit("Unexpected MCPB command; refusing to launch it")
    server = StdioServerParameters(**config, env=dict(os.environ))
    async with (
        asyncio.timeout(30),
        stdio_client(server) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        hits = await session.call_tool(
            "find",
            {"query": "blue observatory", "collection": "release-smoke", "limit": 1},
        )
        if hits.is_error or not any(
            isinstance(content, TextContent) and canary in content.text
            for content in hits.content
        ):
            raise SystemExit("MCPB find did not return the release canary")


if __name__ == "__main__":
    asyncio.run(smoke_bundle(*sys.argv[1:]))
