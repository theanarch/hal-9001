"""The MCP server: three read-only tools over one folder of public documents."""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from .readers import UnsupportedDocumentError, is_supported, read_text
from .sandbox import DocumentRoot, PathOutsideRootError

ROOT_ENV_VAR = "HAL_DOCS_ROOT"

# Every tool only reads, and only from the local folder: tell clients so.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


class DocumentInfo(BaseModel):
    path: str = Field(description="Path relative to the document folder, with forward slashes.")
    size_bytes: int
    modified: str = Field(description="Last modified time, ISO 8601 in UTC.")


class DocumentList(BaseModel):
    folder: str = Field(description="The subfolder that was listed ('' is the document folder itself).")
    documents: list[DocumentInfo]


class DocumentContent(BaseModel):
    path: str
    content: str
    total_chars: int
    truncated: bool = Field(description="True if content was cut at max_chars.")


class SearchHit(BaseModel):
    path: str
    line: int = Field(description="1-based line number (paragraph number for Word files).")
    snippet: str


class SearchResults(BaseModel):
    query: str
    hits: list[SearchHit]
    truncated: bool = Field(description="True if more hits existed than max_results.")


def create_server(root: str | Path) -> MCPServer:
    """Build a server bound to one document folder. Kept separate from main() so tests can use a temp folder."""
    docs = DocumentRoot(root)

    mcp = MCPServer(
        name="hal-documents",
        title="HAL documents",
        version="0.1.0",
        instructions=(
            "Read-only access to a folder of public documents (.txt, .md and Word .docx). "
            "Use list_documents to see what exists, search_documents to find text, "
            "and read_document to read a file. Paths are relative to the document folder."
        ),
    )

    def iter_documents(folder: Path):
        """Yield supported files under folder, skipping hidden entries and anything that links outside the root."""
        for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name.startswith(".") or not is_supported(path) or not docs.is_inside(path):
                    continue
                yield path

    def safe_path(relative: str) -> Path:
        try:
            return docs.resolve(relative)
        except PathOutsideRootError as exc:
            raise ToolError(f"Access denied: {exc}") from exc

    @mcp.tool(annotations=READ_ONLY)
    def list_documents(subfolder: str = "") -> DocumentList:
        """List readable documents in the document folder, recursively.

        Args:
            subfolder: Optional subfolder to list, relative to the document folder. Empty lists everything.
        """
        folder = safe_path(subfolder)
        if not folder.is_dir():
            raise ToolError(f"No such folder: {subfolder!r}")
        documents = []
        for path in iter_documents(folder):
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds")
            documents.append(DocumentInfo(path=docs.relative(path), size_bytes=stat.st_size, modified=modified))
        return DocumentList(folder=subfolder, documents=documents)

    @mcp.tool(annotations=READ_ONLY)
    def read_document(path: str, max_chars: int = Field(default=50_000, ge=1, le=500_000)) -> DocumentContent:
        """Read the text of one document (.txt, .md or Word .docx).

        Args:
            path: Path relative to the document folder, as returned by list_documents.
            max_chars: Return at most this many characters; check `truncated` in the result.
        """
        target = safe_path(path)
        if not target.is_file():
            raise ToolError(f"No such document: {path!r}")
        try:
            text = read_text(target)
        except UnsupportedDocumentError as exc:
            raise ToolError(str(exc)) from exc
        return DocumentContent(
            path=docs.relative(target),
            content=text[:max_chars],
            total_chars=len(text),
            truncated=len(text) > max_chars,
        )

    @mcp.tool(annotations=READ_ONLY)
    def search_documents(
        query: str = Field(min_length=1),
        max_results: int = Field(default=20, ge=1, le=200),
    ) -> SearchResults:
        """Search all documents for a word or phrase (case-insensitive) and return matching lines.

        Args:
            query: Text to look for.
            max_results: Maximum number of matching lines to return.
        """
        needle = query.casefold()
        hits: list[SearchHit] = []
        for path in iter_documents(docs.path):
            try:
                text = read_text(path)
            except (UnsupportedDocumentError, OSError, ValueError):
                continue  # A broken or oversized file should not stop the whole search.
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    if len(hits) == max_results:
                        return SearchResults(query=query, hits=hits, truncated=True)
                    hits.append(SearchHit(path=docs.relative(path), line=number, snippet=_snippet(line, needle)))
        return SearchResults(query=query, hits=hits, truncated=False)

    return mcp


def _snippet(line: str, needle: str, width: int = 160) -> str:
    """A window of the line around the first match, so long paragraphs stay readable."""
    line = line.strip()
    if len(line) <= width:
        return line
    start = max(line.casefold().find(needle) - width // 3, 0)
    piece = line[start : start + width]
    return ("…" if start > 0 else "") + piece + ("…" if start + width < len(line) else "")


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server giving read-only access to a folder of documents.")
    parser.add_argument("--root", default=os.environ.get(ROOT_ENV_VAR), help=f"Document folder (or set {ROOT_ENV_VAR}).")
    args = parser.parse_args()
    if not args.root:
        parser.error(f"Give the document folder with --root or the {ROOT_ENV_VAR} environment variable.")
    # stdio transport: the client starts this process and talks JSON-RPC over stdin/stdout.
    create_server(args.root).run("stdio")


if __name__ == "__main__":
    main()
