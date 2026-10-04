"""Win32 security calls through ctypes: owners, DACLs, private directories and DPAPI.

Only `private_fs` imports this module, and only on Windows. The assert keeps
mypy from checking it against POSIX stubs; CI checks it with --platform win32.
"""

from __future__ import annotations

import ctypes
import msvcrt
import sys
from collections.abc import Callable
from ctypes import wintypes
from functools import cache
from pathlib import Path
from typing import Any

assert sys.platform == "win32"

# (kind, sid, mask, inherit_only): kind is allow, deny or other; an
# inherit-only entry does not apply to the object itself, only to what is
# created inside it later.
AccessEntry = tuple[str, str | None, int, bool]

_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_TOKEN_ELEVATION = 20
_ACL_SIZE_INFORMATION = 2
_SDDL_REVISION_1 = 1
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8
_INHERIT_ONLY_ACE = 0x08
# ACCESS_ALLOWED and ACCESS_ALLOWED_CALLBACK keep the SID right after the mask.
_ALLOWED_ACE_TYPES = frozenset({0x0, 0x9})
# ACCESS_DENIED, ACCESS_DENIED_OBJECT, ACCESS_DENIED_CALLBACK, ACCESS_DENIED_CALLBACK_OBJECT.
_DENIED_ACE_TYPES = frozenset({0x1, 0x6, 0xA, 0xC})


class _AclSizeInformation(ctypes.Structure):
    _fields_ = (
        ("AceCount", wintypes.DWORD),
        ("AclBytesInUse", wintypes.DWORD),
        ("AclBytesFree", wintypes.DWORD),
    )


class _AceHeader(ctypes.Structure):
    _fields_ = (
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", wintypes.WORD),
    )


class _SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    )


class _DataBlob(ctypes.Structure):
    _fields_ = (
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    )


def _function(library: ctypes.WinDLL, name: str, restype: Any, *argtypes: Any) -> Any:
    function = getattr(library, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_out_pointer = ctypes.POINTER(ctypes.c_void_p)

_GetSecurityInfo = _function(
    _advapi32,
    "GetSecurityInfo",
    wintypes.DWORD,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.DWORD,
    _out_pointer,
    _out_pointer,
    _out_pointer,
    _out_pointer,
    _out_pointer,
)
_GetNamedSecurityInfoW = _function(
    _advapi32,
    "GetNamedSecurityInfoW",
    wintypes.DWORD,
    wintypes.LPCWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    _out_pointer,
    _out_pointer,
    _out_pointer,
    _out_pointer,
    _out_pointer,
)
_ConvertSidToStringSidW = _function(
    _advapi32,
    "ConvertSidToStringSidW",
    wintypes.BOOL,
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.LPWSTR),
)
_GetAclInformation = _function(
    _advapi32,
    "GetAclInformation",
    wintypes.BOOL,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_int,
)
_GetAce = _function(_advapi32, "GetAce", wintypes.BOOL, ctypes.c_void_p, wintypes.DWORD, _out_pointer)
_OpenProcessToken = _function(
    _advapi32,
    "OpenProcessToken",
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
)
_GetTokenInformation = _function(
    _advapi32,
    "GetTokenInformation",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
)
_ConvertStringSecurityDescriptorToSecurityDescriptorW = _function(
    _advapi32,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    wintypes.BOOL,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    _out_pointer,
    ctypes.POINTER(wintypes.ULONG),
)
_CryptProtectData = _function(
    _crypt32,
    "CryptProtectData",
    wintypes.BOOL,
    ctypes.POINTER(_DataBlob),
    wintypes.LPCWSTR,
    ctypes.POINTER(_DataBlob),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DataBlob),
)
_CryptUnprotectData = _function(
    _crypt32,
    "CryptUnprotectData",
    wintypes.BOOL,
    ctypes.POINTER(_DataBlob),
    ctypes.POINTER(wintypes.LPWSTR),
    ctypes.POINTER(_DataBlob),
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(_DataBlob),
)
_GetCurrentProcess = _function(_kernel32, "GetCurrentProcess", wintypes.HANDLE)
_CloseHandle = _function(_kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_MoveFileExW = _function(_kernel32, "MoveFileExW", wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
_LocalFree = _function(_kernel32, "LocalFree", ctypes.c_void_p, ctypes.c_void_p)
_CreateDirectoryW = _function(
    _kernel32,
    "CreateDirectoryW",
    wintypes.BOOL,
    wintypes.LPCWSTR,
    ctypes.POINTER(_SecurityAttributes),
)


def _last_error() -> OSError:
    return ctypes.WinError(ctypes.get_last_error())


def _sid_string(sid: int | None) -> str:
    text = wintypes.LPWSTR()
    if not _ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _last_error()
    try:
        return str(text.value)
    finally:
        _LocalFree(ctypes.cast(text, ctypes.c_void_p))


@cache
def current_user_sid() -> str:
    """The SID of the user this process runs as, as S-1-5-21-..."""

    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _last_error()
    try:
        size = wintypes.DWORD()
        # The first call only reports the buffer size it needs.
        _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not _GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise _last_error()
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the SID.
        return _sid_string(ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        _CloseHandle(token)


def is_elevated() -> bool:
    """Whether this process runs with an elevated (administrator) token."""

    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _last_error()
    try:
        elevated = wintypes.DWORD()
        size = wintypes.DWORD()
        if not _GetTokenInformation(
            token, _TOKEN_ELEVATION, ctypes.byref(elevated), ctypes.sizeof(elevated), ctypes.byref(size)
        ):
            raise _last_error()
        return bool(elevated.value)
    finally:
        _CloseHandle(token)


def _access_entries(dacl: int) -> list[AccessEntry]:
    size = _AclSizeInformation()
    if not _GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), _ACL_SIZE_INFORMATION):
        raise _last_error()
    entries: list[AccessEntry] = []
    for index in range(size.AceCount):
        ace = ctypes.c_void_p()
        if not _GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
            raise _last_error()
        header = _AceHeader.from_address(ace.value)
        inherit_only = bool(header.AceFlags & _INHERIT_ONLY_ACE)
        if header.AceType in _ALLOWED_ACE_TYPES:
            mask_address = ace.value + ctypes.sizeof(_AceHeader)
            mask = wintypes.DWORD.from_address(mask_address).value
            sid = _sid_string(mask_address + ctypes.sizeof(wintypes.DWORD))
            entries.append(("allow", sid, int(mask), inherit_only))
        elif header.AceType in _DENIED_ACE_TYPES:
            entries.append(("deny", None, 0, inherit_only))
        else:
            entries.append(("other", None, 0, inherit_only))
    return entries


def _security(read: Callable[[Any, Any, Any], int]) -> tuple[str | None, list[AccessEntry] | None]:
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = read(ctypes.byref(owner), ctypes.byref(dacl), ctypes.byref(descriptor))
    if status:
        raise ctypes.WinError(status)
    try:
        owner_sid = _sid_string(owner.value) if owner.value else None
        # A missing DACL grants everyone full access.
        return owner_sid, _access_entries(dacl.value) if dacl.value else None
    finally:
        _LocalFree(descriptor)


_REQUESTED = _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION


def handle_security(fd: int) -> tuple[str | None, list[AccessEntry] | None]:
    """Owner SID and effective DACL entries of an open file."""

    handle = msvcrt.get_osfhandle(fd)
    return _security(
        lambda owner, dacl, descriptor: _GetSecurityInfo(
            handle, _SE_FILE_OBJECT, _REQUESTED, owner, None, dacl, None, descriptor
        )
    )


def path_security(path: Path) -> tuple[str | None, list[AccessEntry] | None]:
    """Owner SID and effective DACL entries of a file or directory."""

    return _security(
        lambda owner, dacl, descriptor: _GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _REQUESTED, owner, None, dacl, None, descriptor
        )
    )


def replace(source: str, target: Path) -> None:
    """os.replace that returns only once the rename is on disk.

    NTFS journals the rename but may still lose it on power loss; the token
    store has no directory fsync on Windows to make up for it.
    """

    if not _MoveFileExW(source, str(target), _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
        raise _last_error()


def create_private_directory(path: Path) -> None:
    """Create `path` open only to the current user and SYSTEM.

    The protected DACL is set by CreateDirectoryW itself, so the directory
    never exists with the inherited, possibly shared, DACL of its parent.
    Raises FileExistsError when `path` already exists.
    """

    sddl = f"D:P(A;OICI;FA;;;{current_user_sid()})(A;OICI;FA;;;SY)"
    descriptor = ctypes.c_void_p()
    if not _ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None
    ):
        raise _last_error()
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, False)
        if not _CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise _last_error()
    finally:
        _LocalFree(descriptor)


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _take_blob(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        _LocalFree(ctypes.cast(blob.pbData, ctypes.c_void_p))


def protect(data: bytes, *, entropy: bytes) -> bytes:
    """Encrypt `data` with DPAPI for the current Windows user."""

    source, _source_buffer = _blob(data)
    salt, _salt_buffer = _blob(entropy)
    sealed = _DataBlob()
    if not _CryptProtectData(
        ctypes.byref(source), None, ctypes.byref(salt), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(sealed)
    ):
        raise _last_error()
    return _take_blob(sealed)


def unprotect(data: bytes, *, entropy: bytes) -> bytes:
    """Decrypt DPAPI data; fails for another user, other entropy or changed bytes."""

    source, _source_buffer = _blob(data)
    salt, _salt_buffer = _blob(entropy)
    opened = _DataBlob()
    if not _CryptUnprotectData(
        ctypes.byref(source), None, ctypes.byref(salt), None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(opened)
    ):
        raise _last_error()
    return _take_blob(opened)
