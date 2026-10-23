"""End to end: start the server as a separate process and talk to it over stdio, like Claude Desktop does."""

import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio


# "legacy" is the initialize handshake (protocol 2025-11-25) most hosts use today;
# "auto" lets the SDK pick the newest version both sides support (2026-07-28 with mcp 2.x).
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_server_over_stdio(docs_root: Path, mode: str):
    params = StdioServerParameters(command=sys.executable, args=["-m", "hal_9001", "--root", str(docs_root)])
    async with Client(params, mode=mode) as client:
        names = sorted(t.name for t in (await client.list_tools()).tools)
        assert names == ["list_documents", "read_document", "search_documents"]
        result = await client.call_tool("read_document", {"path": "welcome.txt"})
        assert "public archive" in result.structured_content["content"]
