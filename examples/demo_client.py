"""A minimal MCP client: start the hal-documents server, discover its tools and call each one.

This is what Claude Desktop, VS Code or any other MCP host does under the hood.

    uv run python examples/demo_client.py sample-docs
    uv run python examples/demo_client.py "C:\\path\\to\\your\\documents" --query budget
"""

import argparse
import json
import sys

import anyio
from mcp import Client, StdioServerParameters


async def run(root: str, query: str) -> None:
    # 1. Describe how to start the server. The client launches it as a child process
    #    and talks JSON-RPC to it over the process's stdin/stdout.
    server = StdioServerParameters(command=sys.executable, args=["-m", "hal_9001", "--root", root])

    # 2. Connect. Entering the context starts the process and agrees on a protocol version.
    async with Client(server) as client:
        # 3. Discovery: ask the server what it can do. A model only ever sees these
        #    names, descriptions and JSON schemas, never the Python code behind them.
        tools = (await client.list_tools()).tools
        print("Tools offered by the server:")
        for tool in tools:
            print(f"  - {tool.name}: {tool.description.splitlines()[0]}")

        # 4. Calls. An AI host decides these from the conversation; here we hard-code them.
        listing = await client.call_tool("list_documents", {})
        documents = listing.structured_content["documents"]
        print(f"\nlist_documents -> {len(documents)} document(s)")
        for doc in documents:
            print(f"  {doc['path']} ({doc['size_bytes']} bytes)")

        if documents:
            first = documents[0]["path"]
            read = await client.call_tool("read_document", {"path": first, "max_chars": 300})
            print(f"\nread_document({first!r}) ->")
            print(read.structured_content["content"])

        search = await client.call_tool("search_documents", {"query": query})
        print(f"\nsearch_documents({query!r}) ->")
        print(json.dumps(search.structured_content, indent=2, ensure_ascii=False))

        # 5. The sandbox in action: the server refuses, and the client gets an error result.
        escape = await client.call_tool("read_document", {"path": "../../Windows/win.ini"})
        print(f"\nread_document('../../Windows/win.ini') -> is_error={escape.is_error}")
        print(f"  {escape.content[0].text}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", help="Document folder to serve")
    parser.add_argument("--query", default="policy", help="Text to search for")
    args = parser.parse_args()
    anyio.run(run, args.root, args.query)


if __name__ == "__main__":
    main()
