"""Exercise the server through a real MCP client, connected in-process."""

import os
from pathlib import Path

import pytest
from mcp import Client

from hal_9001 import create_server

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(docs_root: Path):
    async with Client(create_server(docs_root)) as c:
        yield c


async def test_exposes_three_read_only_tools(client: Client):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"list_documents", "read_document", "search_documents"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.output_schema is not None


async def test_list_documents_skips_hidden_and_unsupported(client: Client):
    result = await client.call_tool("list_documents", {})
    paths = [d["path"] for d in result.structured_content["documents"]]
    assert paths == ["legacy.txt", "notes.md", "welcome.txt", "policies/travel.docx"]


async def test_list_documents_in_subfolder(client: Client):
    result = await client.call_tool("list_documents", {"subfolder": "policies"})
    assert [d["path"] for d in result.structured_content["documents"]] == ["policies/travel.docx"]


async def test_read_text_and_markdown(client: Client):
    result = await client.call_tool("read_document", {"path": "notes.md"})
    assert not result.is_error
    assert "budget meeting" in result.structured_content["content"]


async def test_read_word_document_including_tables(client: Client):
    content = (await client.call_tool("read_document", {"path": "policies/travel.docx"})).structured_content["content"]
    assert "economy class" in content
    assert "Per diem | 450 SEK" in content


async def test_read_cp1252_text(client: Client):
    content = (await client.call_tool("read_document", {"path": "legacy.txt"})).structured_content["content"]
    assert content == "Årsredovisning för föreningen"


async def test_read_truncates_at_max_chars(client: Client):
    data = (await client.call_tool("read_document", {"path": "welcome.txt", "max_chars": 7})).structured_content
    assert data["content"] == "Welcome"
    assert data["truncated"] is True
    assert data["total_chars"] > 7


async def test_read_errors_are_reported_to_the_model(client: Client):
    for path, message in [("missing.txt", "No such document"), ("image.png", "Unsupported file type")]:
        result = await client.call_tool("read_document", {"path": path})
        assert result.is_error
        assert message in result.content[0].text


async def test_search_is_case_insensitive_across_formats(client: Client):
    hits = (await client.call_tool("search_documents", {"query": "BUDGET"})).structured_content["hits"]
    assert [(h["path"], h["line"]) for h in hits] == [("notes.md", 3)]

    hits = (await client.call_tool("search_documents", {"query": "economy"})).structured_content["hits"]
    assert hits[0]["path"] == "policies/travel.docx"


async def test_search_never_returns_the_outside_secret(client: Client):
    hits = (await client.call_tool("search_documents", {"query": "secret"})).structured_content["hits"]
    assert hits == []


async def test_search_respects_max_results(client: Client):
    data = (await client.call_tool("search_documents", {"query": "e", "max_results": 2})).structured_content
    assert len(data["hits"]) == 2
    assert data["truncated"] is True


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_document", {"path": "../secret.txt"}),
        ("read_document", {"path": "policies/../../secret.txt"}),
        ("list_documents", {"subfolder": ".."}),
        ("read_document", {"path": str(Path(__file__).resolve())}),
    ],
)
async def test_every_tool_refuses_paths_outside_the_folder(client: Client, tool: str, args: dict):
    result = await client.call_tool(tool, args)
    assert result.is_error
    assert "Access denied" in result.content[0].text
    assert "TOP SECRET" not in result.content[0].text


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs extra rights on Windows")
async def test_symlinks_out_of_the_folder_are_invisible(docs_root: Path):
    (docs_root / "secret-link.txt").symlink_to(docs_root.parent / "secret.txt")
    async with Client(create_server(docs_root)) as c:
        listed = (await c.call_tool("list_documents", {})).structured_content["documents"]
        assert "secret-link.txt" not in [d["path"] for d in listed]
        assert (await c.call_tool("read_document", {"path": "secret-link.txt"})).is_error
