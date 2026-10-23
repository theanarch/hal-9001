"""Turning supported document formats into plain text."""

from __future__ import annotations

from pathlib import Path

from docx import Document

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".log"}
WORD_SUFFIXES = {".docx"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | WORD_SUFFIXES

# Files larger than this are refused outright, so one huge file cannot stall the server.
MAX_FILE_BYTES = 20 * 1024 * 1024


class UnsupportedDocumentError(ValueError):
    """Raised for files the server does not know how to read."""


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES


def read_text(path: Path) -> str:
    """Return the text content of a supported document."""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise UnsupportedDocumentError(f"Unsupported file type {suffix or '(none)'!r}. Supported: {supported}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise UnsupportedDocumentError(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    if suffix in WORD_SUFFIXES:
        return _read_docx(path)
    return _read_plain(path)


def _read_plain(path: Path) -> str:
    raw = path.read_bytes()
    # Most files are UTF-8 (utf-8-sig also strips the BOM Notepad likes to add).
    # Older Windows files with Swedish characters are usually cp1252, which never fails to decode.
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _read_docx(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(parts)
