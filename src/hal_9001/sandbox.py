"""Path sandboxing: every path a client sends is checked here before it touches the disk.

The rule is simple: a requested path is joined to the document root, fully resolved
(``..`` segments collapsed, symlinks and Windows junctions followed), and accepted only
if the result is still inside the resolved root. Everything else is rejected.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


class PathOutsideRootError(ValueError):
    """Raised when a requested path would leave the document root."""


class DocumentRoot:
    """A folder that clients may read from, and nothing outside it."""

    def __init__(self, root: str | Path) -> None:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise NotADirectoryError(f"Document root is not a folder: {resolved}")
        self.path = resolved

    def resolve(self, relative: str) -> Path:
        """Turn a client-supplied relative path into a safe absolute path inside the root.

        Raises PathOutsideRootError for absolute paths, drive-qualified paths, NUL bytes,
        and anything that resolves (after following links) to a location outside the root.
        """
        relative = relative.strip()
        if "\x00" in relative:
            raise PathOutsideRootError("Path contains a NUL byte.")
        # Reject absolute paths in both Windows and POSIX form, whatever OS we run on,
        # so "C:\\Windows", "\\\\server\\share", "D:file" and "/etc" all fail the same way.
        win, posix = PureWindowsPath(relative), PurePosixPath(relative)
        if win.drive or win.root or posix.is_absolute():
            raise PathOutsideRootError(f"Absolute paths are not allowed: {relative!r}")

        candidate = (self.path / relative).resolve(strict=False)
        if not candidate.is_relative_to(self.path):
            raise PathOutsideRootError(f"Path is outside the document folder: {relative!r}")
        return candidate

    def relative(self, absolute: Path) -> str:
        """Express a path inside the root the way clients should see it: relative, with forward slashes."""
        return absolute.relative_to(self.path).as_posix()

    def is_inside(self, path: Path) -> bool:
        """True if ``path`` really lives inside the root once links are followed."""
        try:
            return path.resolve(strict=True).is_relative_to(self.path)
        except OSError:
            return False
