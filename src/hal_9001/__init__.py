"""hal-9001: MCP servers that give an AI agent guarded access to documents and SQL Server."""

from .server import create_server, main

__all__ = ["create_server", "main"]
