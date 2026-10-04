"""Markers and checks for tests that depend on one operating system's file semantics."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from kwork_mcp import private_fs

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX modes, uids, umask, symlinks or fd-relative directory walks",
)
windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows ACLs, DPAPI or msvcrt locks",
)


def assert_private_file(path: Path) -> None:
    """Mode 0600 on POSIX; an owner-only owner and DACL on Windows."""

    info = path.stat()
    if sys.platform == "win32":
        assert private_fs.file_problem(info, path=path) is None
    else:
        assert stat.S_IMODE(info.st_mode) == 0o600


def assert_private_directory(path: Path) -> None:
    """Mode 0700 on POSIX; an owner-only owner and DACL on Windows."""

    if sys.platform == "win32":
        assert private_fs.directory_problem(path) is None
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
