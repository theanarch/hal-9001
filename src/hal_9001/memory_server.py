"""The hal-memory MCP server: lets an AI agent create tables and keep data in SQL Server, within guardrails."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from .memory_store import DEFAULT_MAX_DELETES, MAX_ROWS, NAME_PATTERN, Column, Scalar, StoreError, TableStore

CONNECTION_ENV_VAR = "HAL_MEMORY_CONNECTION_STRING"
MAX_DELETES_ENV_VAR = "HAL_MEMORY_MAX_DELETES"

# Hints to the host about what each tool does. Hosts can use destructive_hint to ask the user before running it.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ADDS_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
CHANGES_DATA = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

TableName = Field(pattern=NAME_PATTERN, description="Table name: letters, digits and _, starting with a letter.")
RowId = Field(ge=1, description="The row's Id, from query_rows.")


class ColumnSpec(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, description="Column name: letters, digits and _, starting with a letter.")
    type: Literal["text", "long_text", "integer", "decimal", "boolean", "date", "datetime"] = Field(
        description="text = up to 400 characters, long_text = up to 4000, date/datetime as ISO 8601 strings."
    )
    required: bool = Field(default=False, description="True if every row must have a value.")


class TableInfo(BaseModel):
    name: str
    columns: list[ColumnSpec]


class TableList(BaseModel):
    tables: list[TableInfo]


class RowResult(BaseModel):
    table: str
    row: dict[str, Any]


class RowList(BaseModel):
    table: str
    rows: list[dict[str, Any]]


def create_memory_server(store: TableStore) -> MCPServer:
    mcp = MCPServer(
        name="hal-memory",
        title="HAL memory",
        version="0.2.0",
        instructions=(
            "Tables in SQL Server that you can create and fill on the user's request. Call list_tables first to "
            "see what exists. Every table automatically has Id, CreatedAt, UpdatedAt and DeletedAt. Inserts, "
            "updates and deletes affect exactly one row per call, deletions are limited per session, and tables "
            "cannot be dropped or altered. Only change or delete rows the user explicitly asked about."
        ),
    )

    def table_info(table) -> TableInfo:
        return TableInfo(
            name=table.name,
            columns=[ColumnSpec(name=c.name, type=c.type, required=c.required) for c in table.columns],
        )

    @mcp.tool(annotations=READ_ONLY)
    def list_tables() -> TableList:
        """List the tables you can use, with their columns."""
        return TableList(tables=[table_info(t) for t in _run(store.list_tables)])

    @mcp.tool(annotations=ADDS_DATA)
    def create_table(
        table: str = TableName,
        columns: list[ColumnSpec] = Field(min_length=1, max_length=30),
    ) -> TableInfo:
        """Create a new table. Id, CreatedAt, UpdatedAt and DeletedAt are added automatically.

        Tables cannot be renamed, changed or dropped afterwards, so confirm the design with the user first.

        Args:
            table: Name of the new table.
            columns: The columns to create, in order (1 to 30).
        """
        spec = [Column(c.name, c.type, c.required) for c in columns]
        return table_info(_run(store.create_table, table, spec))

    @mcp.tool(annotations=READ_ONLY)
    def query_rows(
        table: str = TableName,
        where: dict[str, Scalar] | None = Field(default=None, description="Exact matches, e.g. {\"Status\": \"open\"}."),
        contains: str = Field(default="", max_length=200, description="Text to look for in all text columns."),
        limit: int = Field(default=20, ge=1, le=MAX_ROWS),
    ) -> RowList:
        """Read rows from a table, newest first. Deleted rows are never returned.

        Args:
            table: The table to read.
            where: Optional exact-match filters by column name.
            contains: Optional text to search for in the table's text columns.
            limit: Maximum number of rows (at most 100).
        """
        return RowList(table=table, rows=_run(store.query, table, where, contains, limit))

    @mcp.tool(annotations=ADDS_DATA)
    def insert_row(table: str = TableName, values: dict[str, Scalar] = Field(min_length=1)) -> RowResult:
        """Add one row to a table.

        Args:
            table: The table to add to.
            values: Column values by column name, e.g. {"Title": "Buy milk", "Done": false}.
        """
        return RowResult(table=table, row=_run(store.insert, table, values))

    @mcp.tool(annotations=CHANGES_DATA)
    def update_row(table: str = TableName, row_id: int = RowId, values: dict[str, Scalar] = Field(min_length=1)) -> RowResult:
        """Change columns of one existing row.

        Args:
            table: The table the row is in.
            row_id: The row's Id.
            values: The columns to change and their new values.
        """
        return RowResult(table=table, row=_run(store.update, table, row_id, values))

    @mcp.tool(annotations=CHANGES_DATA)
    def delete_row(table: str = TableName, row_id: int = RowId) -> RowResult:
        """Delete one row. Only delete rows the user explicitly asked to remove.

        Args:
            table: The table the row is in.
            row_id: The row's Id.
        """
        return RowResult(table=table, row=_run(store.delete, table, row_id))

    return mcp


def _run(operation, *args):
    """Call the store and turn expected failures into tool errors the model can read."""
    try:
        return operation(*args)
    except StoreError as exc:
        raise ToolError(str(exc)) from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for AI-managed tables in SQL Server.")
    parser.add_argument(
        "--connection-string",
        default=os.environ.get(CONNECTION_ENV_VAR),
        help=f"SQL Server connection string (or set {CONNECTION_ENV_VAR}).",
    )
    parser.add_argument(
        "--max-deletes",
        type=int,
        default=int(os.environ.get(MAX_DELETES_ENV_VAR, DEFAULT_MAX_DELETES)),
        help="Maximum number of deletions per session.",
    )
    args = parser.parse_args()
    if not args.connection_string:
        parser.error(f"Give a connection string with --connection-string or the {CONNECTION_ENV_VAR} variable.")

    store = TableStore(args.connection_string, max_deletes=args.max_deletes)
    try:
        store.check_connection()
    except StoreError as exc:
        # Keep running: the tools will report the problem to the model, so the user sees it in the chat.
        # stderr ends up in the host's MCP log (Claude Desktop: Settings > Developer > Open Logs Folder).
        print(f"Warning: {exc}", file=sys.stderr)
    create_memory_server(store).run("stdio")


if __name__ == "__main__":
    main()
