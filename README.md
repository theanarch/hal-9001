# hal-9001

*"I'm sorry, Dave. I'm afraid I can't do that."* An AI agent that can only do what it is allowed to.

hal-9001 is a set of small [Model Context Protocol](https://modelcontextprotocol.io) (MCP) servers written in Python
with the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (v2). Each one gives an
AI agent a narrow, well-guarded capability:

| Server | Command | What the agent can do |
| --- | --- | --- |
| [hal-documents](#hal-documents-read-only-document-access) | `hal-documents` | Read and search one folder of documents. Nothing else. |
| [hal-memory](#hal-memory-ai-managed-tables-in-sql-server) | `hal-memory` | Create tables in SQL Server and read and write their rows, one row at a time. |

They share one idea: the agent gets a few specific tools instead of general access, and every limit is enforced
in code, and for the database also in SQL Server itself, rather than left to the model.

**Contents:** [Getting started](#getting-started) · [Configuration](#configuration) ·
[Troubleshooting](#troubleshooting) · [Development](#development) · [Design notes](#design-notes) ·
[License](#license)

## hal-documents: read-only document access

| Tool | What it does |
| --- | --- |
| `list_documents` | Lists the readable documents in the folder (optionally one subfolder), recursively. |
| `read_document` | Returns the text of one document, with an optional `max_chars` limit. |
| `search_documents` | Case-insensitive search across all documents; returns file, line and a snippet. |

Supported formats: plain text (`.txt`, `.md`, `.csv`, `.log`) and Word (`.docx`, including tables). Text files
are read as UTF-8 with a fallback to Windows-1252, so older files keep characters like å, ä and ö.

### Security model

The agent can only read, and only inside the configured folder.

- **Path sandbox** ([`sandbox.py`](src/hal_9001/sandbox.py)). Every path from the client is joined to the
  document root and fully resolved, which collapses `..` and follows symlinks and Windows junctions. The
  result must still be inside the resolved root, or the call is refused. Absolute paths in any form
  (`C:\…`, `D:file`, `\\server\share`, `\rooted`, `/etc/…`) and NUL bytes are rejected before touching the disk.
- **Listing and search use the same rule.** Hidden files, unsupported file types and files that link outside
  the root are never listed or searched, so a symlink cannot leak content through search results.
- **Read-only by construction.** No tool writes, moves or deletes anything, and each tool advertises
  `readOnlyHint: true` so hosts can show that to the user.
- **Bounded work.** Large files are refused, and output and result counts are capped.
- **Errors go to the model, not a stack trace.** Expected failures raise `ToolError`, which the SDK returns as
  a tool result with `isError: true` and a clear message the model can act on.

Document *content* is still untrusted input to the model: a document could contain text that looks like
instructions. Hosts should treat tool results as data, which is one more reason the server stays read-only.

## hal-memory: AI-managed tables in SQL Server

The agent can design its own tables and keep data in them, for example a task list, a reading log or notes about
the user. Everything it creates lives in the `ai` schema of a database you choose.

| Tool | What it does | Hint to the host |
| --- | --- | --- |
| `list_tables` | Lists the tables and their columns. | read-only |
| `create_table` | Creates a table from a list of columns (`text`, `long_text`, `integer`, `decimal`, `boolean`, `date`, `datetime`). | additive |
| `query_rows` | Reads rows by exact-match filters and/or text search, newest first, with a row cap. | read-only |
| `insert_row` | Adds one row. | additive |
| `update_row` | Changes one row, by `Id`. | destructive |
| `delete_row` | Deletes one row, by `Id`. | destructive |

Every table gets the same frame automatically: an `Id` primary key, the requested columns, and `CreatedAt`,
`UpdatedAt` and `DeletedAt`.

### Guardrails

The goal: the agent can build and fill tables, but can never drop or empty a table, never touch more rows than
the one it was asked about, and nothing it does is unrecoverable. Four layers enforce that:

1. **No free-form SQL.** The agent names a table, columns and values; it never sends SQL. Every table and column
   name is checked against the database's own catalog (`sys.tables`, `sys.columns`) before it is quoted into a
   statement, and every value is a parameter. Text like `'); DROP TABLE …` is stored as text.
2. **One row per change.** Inserts, updates and deletes affect a single row, chosen by primary key. Each write
   runs in a transaction, and the `OUTPUT` clause reports exactly which rows changed; the transaction commits
   only if that is exactly one row. Reads are capped.
3. **Deletes are soft and capped.** `delete_row` only sets `DeletedAt`; the row stays and can be restored. Each
   server session allows a limited number of deletions, so a confused agent cannot work through a table row by
   row. There is no tool to drop, rename or alter a table.
4. **Least privilege in SQL Server.** The agent's login may only `SELECT`, `INSERT` and `UPDATE` in its schema, is
   explicitly denied `DELETE`, and has no `CREATE TABLE` or `ALTER` rights. Tables are created through one stored
   procedure, `CreateTable`, which runs with the owner's rights (`EXECUTE AS OWNER`), validates names and types
   itself, and only ever builds a `CREATE TABLE` statement from a fixed set of types. The test suite connects as
   this login with raw SQL and confirms that `DELETE`, `TRUNCATE`, `DROP`, `ALTER` and direct `CREATE TABLE` are
   refused by SQL Server, and that SQL injected through the procedure's arguments is rejected.

Layers 1–3 stop mass *updates*; layer 4 makes deleting or dropping impossible even if the Python code were wrong.
The server connects with [mssql-python](https://github.com/microsoft/mssql-python), Microsoft's official driver,
which needs no separate ODBC driver.

## Getting started

These steps are for Windows, where Claude Desktop and SQL Server usually run together. The servers work
independently, so skip step 4 if you only want hal-documents.

### 1. Install the prerequisites

- **Python 3.11 or newer** and **[uv](https://docs.astral.sh/uv/)**. To install uv, run this in PowerShell and
  then open a new terminal:
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **[Claude Desktop](https://claude.ai/download)**, or another MCP host (see [Other hosts](#other-hosts-and-tools)).
- For hal-memory: **SQL Server 2016 or newer** (the free Express or Developer edition is fine) and **SSMS** or
  `sqlcmd`.

### 2. Clone and install

```powershell
git clone https://github.com/theanarch/hal-9001.git
cd hal-9001
uv sync          # creates .venv and installs the dependencies and test tools
uv run pytest    # runs the tests; the SQL Server tests are skipped until you configure one
```

### 3. Try hal-documents

Run the demo client against the bundled sample documents, or any folder of your own. It starts the server and
calls each tool the way an AI host would:

```powershell
uv run python examples/demo_client.py sample-docs --query budget
```

### 4. Set up the hal-memory database

1. **Allow SQL logins.** In SSMS, right-click the server → *Properties* → *Security* → *SQL Server and Windows
   Authentication mode*, then restart the SQL Server service (right-click the server → *Restart*). Many
   installations only allow Windows logins by default, and the agent uses a SQL login.
2. **Create a database** for the agent, with a name of your choice: right-click *Databases* → *New Database*.
3. **Run the setup script.** Open [`sql/setup.sql`](sql/setup.sql), select your database in the database list on
   the toolbar, replace the two `CHANGE_ME` values at the top with a login name and a password of your choice,
   and press *Execute*. From the command line:
   `sqlcmd -S localhost -E -C -d <database> -i sql\setup.sql`

   It creates the `ai` schema, the `CreateTable` procedure and the restricted login, and warns if step 1 is
   still needed. It refuses to run while a placeholder is left or when the selected database is `master`. It is
   safe to run again later, for example after pulling a newer version, and then also resets the login's
   password.
4. **Check the login**, in SSMS with *Connect* → *Database Engine* → *SQL Server Authentication*, or:
   ```powershell
   sqlcmd -S localhost -U <login> -P "<password>" -d <database> -C -Q "SELECT 1"
   ```

For SQL Server Express, use `localhost\SQLEXPRESS` wherever this guide says `localhost`.

### 5. Connect Claude Desktop

Open **Settings → Developer → Edit Config**, which opens `%APPDATA%\Claude\claude_desktop_config.json`. Add the
`mcpServers` entry from [`examples/claude_desktop_config.json`](examples/claude_desktop_config.json) at the
**top level** of the file, next to any keys already there (not inside another block), and fill in:

| Placeholder | Replace with |
| --- | --- |
| `C:\\path\\to\\hal-9001` | The folder you cloned the repository into. |
| `C:\\path\\to\\your\\documents` | The folder hal-documents may read. |
| `<database>`, `<login>`, `<password>` | Your database and the login and password you chose in `sql/setup.sql`. |

Backslashes are written twice (`\\`) in JSON. Leave out a server you don't use. Quit Claude Desktop completely
(also from the system tray) and start it again; under **Settings → Developer** the servers should show as
*running*.

### 6. Try it

Start a new chat and ask, for example:

- *"What do the documents say about the travel policy?"*
- *"Create a table for my reading list with title, author and whether I've finished it."*
- *"Add the book I'm reading now, then show me the list."*

Claude picks tools from their descriptions and asks for your permission before using them.

## Configuration

Each setting can be given as a command-line option (in the host's `args`) or as an environment variable (in the
host's `env`).

| Server | Option | Environment variable | Default | Meaning |
| --- | --- | --- | --- | --- |
| hal-documents | `--root` | `HAL_DOCS_ROOT` | – (required) | The folder the agent may read. |
| hal-memory | `--connection-string` | `HAL_MEMORY_CONNECTION_STRING` | – (required) | SQL Server connection string for the agent's login. |
| hal-memory | `--max-deletes` | `HAL_MEMORY_MAX_DELETES` | `5` | Deletions allowed per server session. |

A connection string looks like
`Server=localhost;Database=<database>;UID=<login>;PWD=<password>;Encrypt=yes;TrustServerCertificate=yes`.
Windows authentication (`Trusted_Connection=yes` instead of `UID`/`PWD`) also works, but then the server runs
with *your* database rights and the least-privilege layer no longer applies.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| *Couldn't load app settings … Expected double-quoted property name* | The config file is no longer valid JSON, usually because `mcpServers` was pasted inside another block. It must sit at the top level, and every `{` needs its `}`. |
| A server shows *failed* or *disconnected* | Open **Settings → Developer → Open Logs Folder** and read the server's `mcp-server-<name>.log`. If `uv` is not found, use its full path as `command`, e.g. `C:\Users\<you>\.local\bin\uv.exe`. |
| `setup.sql` stops with *Run this script in your own database* | Select your database in the database list on the SSMS toolbar (it shows `master` by default) and run it again. |
| *Login failed for user '&lt;login&gt;'* | Run `EXEC xp_readerrorlog 0, 1, N'<login>';` as an administrator to see the reason. *Windows authentication only*: do step 4.1. *Password did not match*: run `setup.sql` again with the password from your connection string. |
| *Cannot connect* or a network error | Check the server name (`localhost\SQLEXPRESS` for Express) and that the SQL Server service is running. |
| The memory tools answer with an error about the connection or `CreateTable` | The server starts even when the database is unreachable or not set up, and reports why on every call. Fix the cause and restart the host. |

A soft-deleted row is restored with `UPDATE ai.<table> SET DeletedAt = NULL WHERE Id = …` as an
administrator.

## Other hosts and tools

### VS Code

[`.vscode/mcp.json`](.vscode/mcp.json) registers hal-documents. Open the repository in VS Code, start the server
from that file, and enter a document folder when prompted (the default is `sample-docs`). Its tools are then
available to the chat in agent mode.

### MCP Inspector

The official Inspector shows exactly what a client sees:

```bash
npx @modelcontextprotocol/inspector uv run hal-documents --root sample-docs
# or headless:
npx @modelcontextprotocol/inspector --cli uv run hal-documents --root sample-docs --method tools/list
```

## Development

```
src/hal_9001/
  server.py         hal-documents: tools, schemas and entry point
  sandbox.py        DocumentRoot: the path check every document tool goes through
  readers.py        Text extraction for plain text and .docx
  memory_server.py  hal-memory: tools, schemas and entry point
  memory_store.py   All SQL for hal-memory, with its guardrails
sql/setup.sql       Schema, CreateTable procedure and least-privilege login for hal-memory
tests/              pytest suites; every server is tested through a real MCP client
examples/           demo_client.py (a minimal MCP client) and a Claude Desktop config
sample-docs/        Small documents for trying hal-documents
```

Run a server by hand (it then waits for a client on stdin/stdout):

```powershell
uv run hal-documents --root sample-docs
uv run hal-memory --connection-string "Server=localhost;Database=<database>;UID=<login>;PWD=<password>;Encrypt=yes;TrustServerCertificate=yes"
```

The SQL Server tests run `sql/setup.sql` with their own test values, then work as the restricted login. Point
them at a server where they may create a test database, for example a throwaway container:

```bash
docker run -d -p 1433:1433 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='<sa-password>' mcr.microsoft.com/mssql/server:2022-latest
HAL_MEMORY_TEST_SERVER=localhost,1433 HAL_MEMORY_TEST_SA_PASSWORD='<sa-password>' uv run pytest
```

CI runs all tests on Linux and Windows, and the SQL Server tests against SQL Server 2022.

### Adding a server

Each server follows the same pattern, so a new one touches only its own files:

1. `src/hal_9001/<name>_server.py` with a `create_<name>_server(...)` factory and a `main()` entry point; keep
   I/O and guardrails in a separate module next to it.
2. A `[project.scripts]` entry in `pyproject.toml`.
3. Tests that drive it through `mcp.Client`, plus an entry in `examples/claude_desktop_config.json`.
4. A row in the table at the top of this README, a section describing its tools and guardrails, and its options
   in [Configuration](#configuration).

## Design notes

- Servers are built by factories (`create_server(root)`, `create_memory_server(store)`) instead of module-level
  globals, so tests can point them at a temporary folder or a test database.
- Tool inputs and outputs are typed. The SDK turns type hints, `Field` constraints and docstrings into the JSON
  Schemas the model sees, and Pydantic return types become `outputSchema` and `structuredContent`.
- Tool annotations (`readOnlyHint`, `destructiveHint`) tell hosts which calls only read and which change data, so
  they can ask the user first. They are hints; the guardrails above are what actually enforce the limits.
- Transport is stdio, the standard for local servers: the host starts the process and owns its lifetime. Logs go
  to stderr, because stdout carries the protocol.
- The servers speak both the current stateless protocol (2026-07-28) and the older initialize handshake
  (2025-11-25), and the tests check both over a real stdio connection.

## License

[MIT](LICENSE)
