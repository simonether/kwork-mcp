"""Owner-only state files and interprocess locks on POSIX and Windows.

POSIX keeps the state private with modes: 0700 directories and 0600 files
owned by the effective uid. Windows mode bits mean nothing, so there the owner
and the DACL are checked instead, new state directories get an owner-only
protected DACL, and stored tokens are sealed with DPAPI: another Windows user
can neither read a token nor plant one.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path, PureWindowsPath

if sys.platform == "win32":
    import msvcrt

    from kwork_mcp import windows
else:
    import fcntl

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
# OWNER RIGHTS: whoever currently owns the object. os.mkdir(path, 0o700) grants it
# on Windows since Python 3.12.4.
OWNER_RIGHTS_SID = "S-1-3-4"
# CREATOR OWNER: replaced by the creator in what a directory passes on.
CREATOR_OWNER_SID = "S-1-3-0"
# NT SERVICE\TrustedInstaller owns the system drive root.
TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
# Rights that let their holder rename, replace or re-permission a directory.
_DELETE = 0x00010000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_FILE_DELETE_CHILD = 0x00000040
_GENERIC_ALL = 0x10000000
# Reparse tags with this bit redirect to another name: symlinks, junctions.
_NAME_SURROGATE_TAG = 0x20000000
_SHARING_VIOLATION_WAIT_SECONDS = 1.0
_SHARING_VIOLATION_ERRORS = frozenset({5, 32, 33})
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


AccessEntries = Sequence[tuple[str, str | None, int, bool]]


def windows_acl_problem(
    owner: str | None,
    entries: AccessEntries | None,
    *,
    user_sid: str,
    directory: bool = False,
) -> str | None:
    """Why a Windows state file or directory is not private to `user_sid`, or None.

    Like OpenSSH for Windows with private keys, it trusts the user, SYSTEM and
    Administrators, who can take any file anyway; OWNER RIGHTS is trusted too,
    because the owner must be one of them. `entries` are the DACL entries as
    (kind, sid, mask, inherit_only); a missing DACL grants everyone access. A
    directory must not pass anyone else on to the files created in it either,
    so its inherit-only entries count too.
    """

    trusted = {user_sid, SYSTEM_SID, ADMINISTRATORS_SID}
    if owner not in trusted:
        return "wrong_owner"
    if entries is None:
        return "acl_not_private"
    allowed = trusted | {OWNER_RIGHTS_SID}
    if directory:
        allowed.add(CREATOR_OWNER_SID)
    for kind, sid, mask, inherit_only in entries:
        if (inherit_only and not directory) or kind == "deny":
            continue
        if kind == "allow" and (mask == 0 or sid in allowed):
            continue
        return "acl_not_private"
    return None


def windows_ancestor_problem(
    owner: str | None,
    entries: AccessEntries | None,
    *,
    user_sid: str,
    root: bool,
) -> str | None:
    """Why someone else could swap a directory above the state directory, or None.

    The Windows counterpart of rejecting group- or world-writable POSIX
    ancestors. The owner can rewrite the DACL, so it must be trusted, and no one
    else may hold DELETE, WRITE_DAC, WRITE_OWNER, FILE_DELETE_CHILD or
    GENERIC_ALL. DELETE on a drive root is harmless: a root cannot be renamed.
    """

    trusted = {user_sid, SYSTEM_SID, ADMINISTRATORS_SID, TRUSTED_INSTALLER_SID}
    if owner not in trusted:
        return "untrusted_ancestor_owner"
    if entries is None:
        return "untrusted_writable_ancestor"
    dangerous = _WRITE_DAC | _WRITE_OWNER | _FILE_DELETE_CHILD | _GENERIC_ALL | (0 if root else _DELETE)
    allowed = trusted | {OWNER_RIGHTS_SID}
    for kind, sid, mask, inherit_only in entries:
        if inherit_only or kind == "deny":
            continue
        if kind == "allow" and (not mask & dangerous or sid in allowed):
            continue
        return "untrusted_writable_ancestor"
    return None


def windows_path_is_ambiguous(path: PureWindowsPath) -> bool:
    """Reject Windows path syntax that may name something other than it reads.

    Only `X:\\...` paths pass: UNC and device paths, drive-relative paths,
    alternate data streams, reserved device names and names that Windows trims
    (trailing dot or space) are refused.
    """

    drive = path.drive
    if len(drive) != 2 or drive[1] != ":" or not drive[0].isascii() or not drive[0].isalpha() or not path.root:
        return True
    for part in path.parts[1:]:
        device = part.split(".", 1)[0].rstrip(" ").upper()
        if ":" in part or part.endswith((".", " ")) or device in _RESERVED_DEVICE_NAMES:
            return True
    return False


def try_lock(fd: int) -> None:
    """Take an exclusive lock on an open file or raise BlockingIOError at once.

    The lock belongs to the open file: closing it or the death of the process
    releases it, so a crash never leaves a stale lock behind.
    """

    if sys.platform == "win32":
        # msvcrt locks bytes from the current position; lock files stay empty.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EDEADLK}:
                raise BlockingIOError(errno.EAGAIN, "the lock is held") from exc
            raise
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def restrict_file(fd: int) -> None:
    """Make an open state file owner-only.

    POSIX sets 0600, which the umask may have narrowed or a stale file may
    lack. On Windows the file inherits the owner-only DACL of the state
    directory and is checked through its handle instead.
    """

    if sys.platform != "win32":
        os.fchmod(fd, 0o600)


def restrict_path(path: Path) -> None:
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def is_link(info: os.stat_result) -> bool:
    """A symlink or, on Windows, any reparse point that redirects, such as a junction."""

    if sys.platform == "win32":
        reparse = info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        redirects = bool(reparse and info.st_reparse_tag & _NAME_SURROGATE_TAG)
    else:
        redirects = False
    return redirects or stat.S_ISLNK(info.st_mode)


def file_problem(info: os.stat_result, *, fd: int | None = None, path: Path | None = None) -> str | None:
    """Why a state file is not private to the current user, or None.

    POSIX reads the owner and mode from `info`: `wrong_owner` or
    `permissions_must_be_0600`. Windows reads the owner and DACL of the open
    file `fd`, or of `path` when nothing is open: `wrong_owner`,
    `acl_not_private` or `acl_unreadable`.
    """

    if sys.platform == "win32":
        problem = _windows_problem(fd=fd, path=path)
    elif info.st_uid != os.geteuid():
        problem = "wrong_owner"
    elif stat.S_IMODE(info.st_mode) != 0o600:
        problem = "permissions_must_be_0600"
    else:
        problem = None
    return problem


def directory_problem(path: Path) -> str | None:  # pragma: no cover - Windows only
    """Why a Windows state directory is not private, as `file_problem` reports it.

    POSIX checks directories during the fd-anchored walk in `security`.
    """

    return _windows_problem(path=path, directory=True)


def ancestor_problem(path: Path, *, root: bool) -> str | None:  # pragma: no cover - Windows only
    """Why someone else could swap the Windows directory `path`, or None."""

    if sys.platform == "win32":
        try:
            owner, entries = windows.path_security(path)
            return windows_ancestor_problem(owner, entries, user_sid=windows.current_user_sid(), root=root)
        except OSError:
            return "acl_unreadable"
    else:
        raise NotImplementedError


def _windows_problem(  # pragma: no cover - Windows only
    *,
    fd: int | None = None,
    path: Path | None = None,
    directory: bool = False,
) -> str | None:
    if sys.platform == "win32":
        try:
            if fd is not None:
                owner, entries = windows.handle_security(fd)
            elif path is not None:
                owner, entries = windows.path_security(path)
            else:
                raise ValueError("fd or path is required")
            return windows_acl_problem(owner, entries, user_sid=windows.current_user_sid(), directory=directory)
        except OSError:
            return "acl_unreadable"
    else:
        raise NotImplementedError


def is_elevated() -> bool:
    """Whether a Windows process runs with an administrator token; always False on POSIX."""

    if sys.platform == "win32":
        elevated = windows.is_elevated()
    else:
        elevated = False
    return elevated


def create_private_directory(path: Path) -> None:  # pragma: no cover - Windows only
    """Create a Windows directory open only to the current user and SYSTEM."""

    if sys.platform == "win32":
        windows.create_private_directory(path)
    else:
        raise NotImplementedError


def fsync_directory(path: Path) -> None:
    """Make a rename or unlink in `path` durable.

    Windows cannot open a directory as a file; NTFS journals the change itself.
    """

    if sys.platform != "win32":
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def replace(source: str, target: Path) -> None:
    """os.replace that waits out a brief Windows sharing violation.

    Windows also writes the rename through to disk, since it has no directory
    fsync to follow it.
    """

    if sys.platform == "win32":
        _wait_out_sharing_violation(lambda: windows.replace(source, target))
    else:
        _wait_out_sharing_violation(lambda: os.replace(source, target))


def unlink(path: Path) -> None:
    _wait_out_sharing_violation(path.unlink)


def _wait_out_sharing_violation(operation: Callable[[], None]) -> None:
    # An antivirus scanner or the search indexer may hold a fresh file open
    # for a moment on Windows; POSIX renames and unlinks never wait.
    if sys.platform == "win32":
        deadline = time.monotonic() + _SHARING_VIOLATION_WAIT_SECONDS
        delay = 0.01
        while True:
            try:
                operation()
                return
            except PermissionError as exc:
                if exc.winerror not in _SHARING_VIOLATION_ERRORS or time.monotonic() >= deadline:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
    else:
        operation()


def seal(data: bytes, *, purpose: str) -> bytes:
    """Bind a stored secret to the current OS user.

    Windows encrypts it with DPAPI, keyed to the user and to `purpose`, so no
    other user can read it and a file copied from another account or user
    fails to open. POSIX stores it as is under mode 0600.
    """

    if sys.platform == "win32":
        sealed = windows.protect(data, entropy=purpose.encode())
    else:
        sealed = data
    return sealed


def unseal(data: bytes, *, purpose: str) -> bytes:
    if sys.platform == "win32":
        opened = windows.unprotect(data, entropy=purpose.encode())
    else:
        opened = data
    return opened
