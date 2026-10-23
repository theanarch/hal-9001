"""Database access for the hal-memory server, with the guardrails built in.

The AI never writes SQL. It names a table, columns and values; this module checks every name against the
database's own catalog, quotes it, and passes every value as a parameter. Tables live in the "ai" schema and are
created only through the ai.CreateTable procedure (see sql/setup.sql). Each write touches exactly one row,
deletes are soft (the row gets a DeletedAt timestamp) and capped per session.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import mssql_python

DEFAULT_MAX_DELETES = 5
MAX_ROWS = 100

SCHEMA = "ai"
SYSTEM_COLUMNS = ("Id", "CreatedAt", "UpdatedAt", "DeletedAt")
NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
_NAME_RE = re.compile(NAME_PATTERN)

# Column types the AI may ask for. ai.CreateTable maps the same names to SQL types.
COLUMN_TYPES = ("text", "long_text", "integer", "decimal", "boolean", "date", "datetime")

Scalar = str | int | float | bool | None


class StoreError(Exception):
    """An expected failure that should be shown to the model, such as an unknown table or a broken guardrail."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    required: bool


@dataclass(frozen=True)
class Table:
    name: str
    columns: list[Column] = field(default_factory=list)


class TableStore:
    def __init__(self, connection_string: str, max_deletes: int = DEFAULT_MAX_DELETES) -> None:
        self._connection_string = connection_string
        self._max_deletes = max_deletes
        self._deletes_done = 0

    def _connect(self) -> mssql_python.Connection:
        # A fresh connection per call keeps things simple and survives SQL Server restarts.
        try:
            return mssql_python.connect(self._connection_string)
        except mssql_python.Error as exc:
            raise StoreError(f"Cannot connect to SQL Server: {exc}") from exc

    def check_connection(self) -> None:
        """Fail with a clear message if the login or the CreateTable procedure is missing."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT OBJECT_ID(?, 'P')", (f"{SCHEMA}.CreateTable",))
            if cursor.fetchall()[0][0] is None:
                raise StoreError(
                    f"Connected, but found no {SCHEMA}.CreateTable procedure this login may use. "
                    "Run sql/setup.sql in the database named in the connection string."
                )
        finally:
            conn.close()

    # --- Tables --------------------------------------------------------------------------------------------------

    def list_tables(self) -> list[Table]:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT t.name, c.name, ty.name, c.max_length, c.is_nullable "
                "FROM sys.tables t "
                "JOIN sys.columns c ON c.object_id = t.object_id "
                "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
                "WHERE t.schema_id = SCHEMA_ID(?) "
                "ORDER BY t.name, c.column_id",
                (SCHEMA,),
            )
            tables: dict[str, Table] = {}
            for table, column, sql_type, max_length, nullable in cursor.fetchall():
                tables.setdefault(table, Table(table))
                if column not in SYSTEM_COLUMNS:
                    tables[table].columns.append(Column(column, _friendly_type(sql_type, max_length), not nullable))
            return list(tables.values())
        finally:
            conn.close()

    def create_table(self, name: str, columns: list[Column]) -> Table:
        _check_name(name, "table")
        for column in columns:
            _check_name(column.name, "column")
        spec = json.dumps([{"name": c.name, "type": c.type, "required": c.required} for c in columns])
        conn = self._connect()
        try:
            conn.cursor().execute(f"EXEC {SCHEMA}.CreateTable @TableName = ?, @Columns = ?", (name, spec))
            conn.commit()
        except mssql_python.Error as exc:
            conn.rollback()
            raise StoreError(_sql_message(exc)) from exc
        finally:
            conn.close()
        return self._table(name)

    def _table(self, name: str) -> Table:
        for table in self.list_tables():
            if table.name.lower() == name.lower():
                return table
        raise StoreError(f"No table named {name!r}. Use list_tables to see the tables, or create_table to make one.")

    # --- Rows ----------------------------------------------------------------------------------------------------

    def query(
        self,
        table: str,
        where: dict[str, Scalar] | None = None,
        contains: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        info = self._table(table)
        filters, params = ["DeletedAt IS NULL"], []
        for column, value in (where or {}).items():
            name = _known_column(info, column, allow_system=True)
            if value is None:
                filters.append(f"{_quote(name)} IS NULL")
            else:
                filters.append(f"{_quote(name)} = ?")
                params.append(value)
        if contains:
            text_columns = [c.name for c in info.columns if c.type in ("text", "long_text")]
            if not text_columns:
                raise StoreError(f"Table {info.name!r} has no text columns to search with 'contains'.")
            pattern = f"%{_escape_like(contains)}%"
            filters.append("(" + " OR ".join(f"{_quote(c)} LIKE ? ESCAPE '\\'" for c in text_columns) + ")")
            params.extend([pattern] * len(text_columns))

        sql = f"SELECT TOP (?) * FROM {_table_ref(info.name)} WHERE {' AND '.join(filters)} ORDER BY Id DESC"
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, (min(limit, MAX_ROWS), *params))
            return [_row_dict(cursor, row) for row in cursor.fetchall()]
        except mssql_python.Error as exc:
            raise StoreError(_sql_message(exc)) from exc
        finally:
            conn.close()

    def insert(self, table: str, values: dict[str, Scalar]) -> dict[str, Any]:
        info = self._table(table)
        names = [_known_column(info, column) for column in values]
        if not names:
            raise StoreError("Give at least one column value.")
        sql = (
            f"INSERT INTO {_table_ref(info.name)} ({', '.join(_quote(n) for n in names)}) "
            f"OUTPUT INSERTED.* VALUES ({', '.join('?' for _ in names)})"
        )
        return self._change_one_row(sql, tuple(values.values()))

    def update(self, table: str, row_id: int, values: dict[str, Scalar]) -> dict[str, Any]:
        info = self._table(table)
        names = [_known_column(info, column) for column in values]
        if not names:
            raise StoreError("Give at least one column value to change.")
        assignments = ", ".join(f"{_quote(n)} = ?" for n in names)
        sql = (
            f"UPDATE {_table_ref(info.name)} SET {assignments}, UpdatedAt = SYSUTCDATETIME() "
            "OUTPUT INSERTED.* WHERE Id = ? AND DeletedAt IS NULL"
        )
        return self._change_one_row(sql, (*values.values(), row_id))

    def delete(self, table: str, row_id: int) -> dict[str, Any]:
        info = self._table(table)
        if self._deletes_done >= self._max_deletes:
            raise StoreError(
                f"Delete limit reached: at most {self._max_deletes} deletions per session. "
                "Ask the user to remove further rows themselves."
            )
        row = self._change_one_row(
            f"UPDATE {_table_ref(info.name)} SET DeletedAt = SYSUTCDATETIME() OUTPUT INSERTED.* "
            "WHERE Id = ? AND DeletedAt IS NULL",
            (row_id,),
        )
        self._deletes_done += 1
        return row

    def _change_one_row(self, sql: str, params: tuple) -> dict[str, Any]:
        """Run one write in a transaction and commit only if exactly one row changed.

        The OUTPUT clause returns every row the statement touched. WHERE Id = ? on the primary key already
        limits each statement to one row; this check is the safety net if the SQL is ever changed.
        """
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            if not rows:
                raise StoreError("No active row with that id. Use query_rows to find the right id.")
            if len(rows) > 1:
                raise StoreError(f"Refused: the change would affect {len(rows)} rows, only 1 is allowed.")
            result = _row_dict(cursor, rows[0])
            conn.commit()
            return result
        except mssql_python.Error as exc:
            conn.rollback()
            raise StoreError(_sql_message(exc)) from exc
        except BaseException:
            conn.rollback()  # Nothing is saved unless we reached commit() above.
            raise
        finally:
            conn.close()


def _check_name(name: str, kind: str) -> None:
    if not _NAME_RE.match(name):
        raise StoreError(f"Invalid {kind} name {name!r}: use 1-64 letters, digits or _, starting with a letter.")


def _known_column(table: Table, column: str, allow_system: bool = False) -> str:
    """Return the column's real name, or refuse. Only names that exist in the table ever reach the SQL."""
    candidates = [c.name for c in table.columns] + (list(SYSTEM_COLUMNS) if allow_system else [])
    for name in candidates:
        if name.lower() == column.lower():
            return name
    if column.lower() in (c.lower() for c in SYSTEM_COLUMNS):
        raise StoreError(f"{column} is managed automatically and cannot be set.")
    known = ", ".join(c.name for c in table.columns)
    raise StoreError(f"Table {table.name!r} has no column {column!r}. Columns: {known}.")


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _table_ref(table: str) -> str:
    return f"{SCHEMA}.{_quote(table)}"


def _friendly_type(sql_type: str, max_length: int) -> str:
    if sql_type == "nvarchar":
        return "long_text" if max_length > 800 else "text"
    return {
        "int": "integer",
        "decimal": "decimal",
        "bit": "boolean",
        "date": "date",
        "datetime2": "datetime",
    }.get(sql_type, sql_type)


def _row_dict(cursor, row) -> dict[str, Any]:
    """Turn a database row into JSON-friendly values."""
    result = {}
    for (name, *_), value in zip(cursor.description, row):
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        result[name] = value
    return result


def _escape_like(text: str) -> str:
    """Make %, _ and [ in the user's text match literally instead of acting as LIKE wildcards."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")


def _sql_message(exc: mssql_python.Error) -> str:
    """Keep the part of a driver error the model can act on, e.g. a THROW message from CreateTable."""
    text = str(exc)
    marker = "[SQL Server]"
    return text.split(marker, 1)[1].strip() if marker in text else text
