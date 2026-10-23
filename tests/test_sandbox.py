import os
from pathlib import Path

import pytest

from hal_9001.sandbox import DocumentRoot, PathOutsideRootError


def test_relative_paths_resolve_inside_root(docs_root: Path):
    root = DocumentRoot(docs_root)
    assert root.resolve("welcome.txt") == docs_root.resolve() / "welcome.txt"
    assert root.resolve("policies/travel.docx") == docs_root.resolve() / "policies" / "travel.docx"
    assert root.resolve("policies/../welcome.txt") == docs_root.resolve() / "welcome.txt"
    assert root.resolve("") == docs_root.resolve()


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../secret.txt",
        "policies/../../secret.txt",
        "./../docs/../secret.txt",
    ],
)
def test_parent_traversal_is_rejected(docs_root: Path, path: str):
    with pytest.raises(PathOutsideRootError):
        DocumentRoot(docs_root).resolve(path)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "D:relative-to-drive.txt",
        "\\\\server\\share\\file.txt",
        "\\rooted-on-current-drive.txt",
    ],
)
def test_absolute_paths_are_rejected(docs_root: Path, path: str):
    with pytest.raises(PathOutsideRootError, match="Absolute"):
        DocumentRoot(docs_root).resolve(path)


def test_nul_byte_is_rejected(docs_root: Path):
    with pytest.raises(PathOutsideRootError):
        DocumentRoot(docs_root).resolve("welcome.txt\x00.png")


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs extra rights on Windows")
def test_symlink_pointing_outside_is_rejected(docs_root: Path):
    (docs_root / "escape").symlink_to(docs_root.parent, target_is_directory=True)
    (docs_root / "secret-link.txt").symlink_to(docs_root.parent / "secret.txt")
    root = DocumentRoot(docs_root)
    with pytest.raises(PathOutsideRootError):
        root.resolve("escape/secret.txt")
    with pytest.raises(PathOutsideRootError):
        root.resolve("secret-link.txt")
    assert not root.is_inside(docs_root / "secret-link.txt")


def test_root_must_exist(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        DocumentRoot(tmp_path / "missing")
