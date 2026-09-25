"""Filesystem, token, logging, and external-data security primitives."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger
from pydantic import BaseModel, JsonValue

from kwork_mcp.config import (
    KworkConfig,
    canonicalize_percent_escape_case,
    contains_unsafe_text_codepoint,
    normalize_proxy_url,
)
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode

_SENSITIVE_KEY_PARTS = (
    "password",
    "token",
    "authorization",
    "cookie",
    "secret",
    "proxy_url",
)
_URL_AUTHORITY = re.compile(
    r"(?P<scheme>https?|socks[45])://(?P<authority>[^\s/?#]+)",
    re.IGNORECASE,
)
_REDACTION_LOCK = threading.RLock()
_RUNTIME_REDACTION_SECRETS: set[str] = set()
_FD_RELEASE_TIMEOUT_SECONDS = 5.0


def _security_error(diagnostic: str) -> GatewayError:
    return GatewayError(ErrorCode.VALIDATION, diagnostic=diagnostic)


def register_redaction_secrets(secrets: Sequence[str]) -> None:
    """Add runtime-discovered credentials to process-wide log redaction."""

    with _REDACTION_LOCK:
        _RUNTIME_REDACTION_SECRETS.update(secret for secret in secrets if secret)


def runtime_redaction_secrets() -> tuple[str, ...]:
    with _REDACTION_LOCK:
        return tuple(_RUNTIME_REDACTION_SECRETS)


def _consume_finished_task(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


async def _finish_task[TaskResultT](
    task: asyncio.Task[TaskResultT],
    *,
    timeout_seconds: float | None = None,
) -> tuple[TaskResultT, bool]:
    """Finish cleanup and report cancellation received while it was shielded."""

    cancelled = False
    loop = asyncio.get_running_loop()
    deadline = None if timeout_seconds is None else loop.time() + max(timeout_seconds, 0.0)
    while True:
        try:
            if deadline is None:
                return await asyncio.shield(task), cancelled
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("fd_cleanup_deadline_exceeded")
            async with asyncio.timeout(remaining):
                return await asyncio.shield(task), cancelled
        except TimeoutError:
            if task.done():
                return task.result(), cancelled
            task.cancel()
            task.add_done_callback(_consume_finished_task)
            raise
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            cancelled = True


async def _release_fd(
    release: Callable[[int], None],
    fd: int,
    *,
    primary_error: BaseException | None,
) -> None:
    """Bound release work without allowing it to replace an active error."""

    release_task = asyncio.create_task(asyncio.to_thread(release, fd))
    cancelled_during_release = False
    release_error: BaseException | None = None
    try:
        _, cancelled_during_release = await _finish_task(
            release_task,
            timeout_seconds=_FD_RELEASE_TIMEOUT_SECONDS,
        )
    except BaseException as exc:
        release_error = exc

    if release_error is not None:
        if primary_error is not None:
            with contextlib.suppress(Exception):
                logger.warning(
                    "fd_release_failed_while_preserving_primary timeout={}",
                    isinstance(release_error, TimeoutError),
                )
            return
        raise release_error
    if cancelled_during_release and primary_error is None:
        raise asyncio.CancelledError


@asynccontextmanager
async def cancellation_safe_fd_guard(
    acquire: Callable[[], int],
    release: Callable[[int], None],
) -> AsyncIterator[None]:
    """Bridge a blocking fd lock without orphaning it on task cancellation."""

    acquire_task = asyncio.create_task(asyncio.to_thread(acquire))
    try:
        fd = await asyncio.shield(acquire_task)
    except asyncio.CancelledError as exc:
        acquired_fd: int | None = None
        with contextlib.suppress(Exception):
            acquired_fd, _ = await _finish_task(acquire_task)
        if acquired_fd is not None:
            await _release_fd(
                release,
                acquired_fd,
                primary_error=exc,
            )
        raise

    body_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        await _release_fd(release, fd, primary_error=body_error)


def _trusted_directory_owners() -> frozenset[int]:
    current_uid = os.geteuid() if hasattr(os, "geteuid") else 0
    return frozenset({0, current_uid})


def _validate_directory_component(
    info: os.stat_result,
    *,
    final: bool,
    parent_is_shared_sticky: bool,
) -> bool:
    """Validate one opened physical directory and return its sticky-shared state."""

    if not stat.S_ISDIR(info.st_mode):
        raise _security_error("state_directory_component_not_directory")
    mode = stat.S_IMODE(info.st_mode)
    current_uid = os.geteuid() if hasattr(os, "geteuid") else info.st_uid
    if final:
        if info.st_uid != current_uid:
            raise _security_error("state_directory_wrong_owner")
        if mode != 0o700:
            raise _security_error("state_directory_permissions_must_be_0700")
        return False
    if info.st_uid not in _trusted_directory_owners():
        raise _security_error("state_directory_untrusted_ancestor_owner")
    shared_writable = bool(mode & 0o022)
    sticky = bool(info.st_mode & stat.S_ISVTX)
    if shared_writable and not sticky:
        raise _security_error("state_directory_untrusted_writable_ancestor")
    if parent_is_shared_sticky and shared_writable:
        raise _security_error("state_directory_nested_shared_writable_ancestor")
    return shared_writable and sticky


def _resolve_trusted_state_aliases(path: Path) -> Path:
    """Resolve trusted ancestor aliases while rejecting ambiguous path syntax."""

    def validate_lexical(candidate: Path, *, user_supplied: bool) -> None:
        raw_path = os.fspath(candidate)
        if (
            not candidate.is_absolute()
            or raw_path.startswith("//")
            or "\x00" in raw_path
            or contains_unsafe_text_codepoint(raw_path)
            or (user_supplied and ".." in candidate.parts)
        ):
            raise _security_error("state_directory_path_invalid")

    validate_lexical(path, user_supplied=True)
    pending = path
    visited_links: set[tuple[int, int]] = set()
    for _hop in range(40):
        current = Path(pending.anchor)
        parts = pending.parts[1:]
        followed_alias = False
        for index, component in enumerate(parts):
            candidate = current / component
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                # Do not weakly resolve a missing tail.  The fd-anchored
                # O_NOFOLLOW walk must observe whatever appears here next.
                return candidate.joinpath(*parts[index + 1 :])
            except OSError as exc:
                raise _security_error(f"state_directory_alias_error:{type(exc).__name__}") from exc
            if not stat.S_ISLNK(info.st_mode):
                current = candidate
                continue
            if index == len(parts) - 1:
                raise _security_error("state_directory_final_symlink")
            try:
                parent_info = current.stat()
            except OSError as exc:
                raise _security_error(f"state_directory_alias_parent_error:{type(exc).__name__}") from exc
            if (
                parent_info.st_uid not in _trusted_directory_owners()
                or stat.S_IMODE(parent_info.st_mode) & 0o022
                or info.st_uid not in _trusted_directory_owners()
            ):
                raise _security_error("state_directory_untrusted_symlink_ancestor")
            link_identity = (info.st_dev, info.st_ino)
            if link_identity in visited_links:
                raise _security_error("state_directory_alias_loop")
            visited_links.add(link_identity)
            try:
                target_text = os.readlink(candidate)
            except OSError as exc:
                raise _security_error(f"state_directory_alias_resolution_error:{type(exc).__name__}") from exc
            target = Path(target_text)
            target_base = target if target.is_absolute() else current / target
            normalized = Path(
                os.path.normpath(
                    os.fspath(target_base.joinpath(*parts[index + 1 :])),
                )
            )
            validate_lexical(normalized, user_supplied=False)
            pending = normalized
            followed_alias = True
            break
        if not followed_alias:
            return current
    raise _security_error("state_directory_alias_limit")


def ensure_secure_directory(path: Path) -> Path:
    """Create a private directory through a trusted, fd-anchored ancestor chain."""

    canonical = _resolve_trusted_state_aliases(path)
    # O_SEARCH (POSIX/macOS) and O_PATH (Linux) need only traversal
    # permission, so a safe execute-only ancestor is not rejected.
    open_flags = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY))
    if hasattr(os, "O_DIRECTORY"):
        open_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    directory_fd = -1
    try:
        directory_fd = os.open(canonical.anchor, open_flags)
        root_info = os.fstat(directory_fd)
        parent_is_shared_sticky = _validate_directory_component(
            root_info,
            final=not canonical.parts[1:],
            parent_is_shared_sticky=False,
        )
        for index, component in enumerate(canonical.parts[1:]):
            final = index == len(canonical.parts[1:]) - 1
            child_fd = -1
            try:
                try:
                    child_fd = os.open(component, open_flags, dir_fd=directory_fd)
                except FileNotFoundError:
                    created = False
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                        created = True
                    except FileExistsError:
                        pass
                    if created:
                        # mkdir mode is filtered by the process umask.  Repair
                        # only the inode we created; a FileExists race is never
                        # chmodded and is instead opened with O_NOFOLLOW below.
                        os.chmod(
                            component,
                            0o700,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    child_fd = os.open(component, open_flags, dir_fd=directory_fd)
                child_info = os.fstat(child_fd)
                child_is_shared_sticky = _validate_directory_component(
                    child_info,
                    final=final,
                    parent_is_shared_sticky=parent_is_shared_sticky,
                )
                os.close(directory_fd)
                directory_fd = child_fd
                child_fd = -1
                parent_is_shared_sticky = child_is_shared_sticky
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
    except GatewayError:
        raise
    except OSError as exc:
        raise _security_error(f"state_directory_chain_error:{type(exc).__name__}") from exc
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
    return canonical


def _validate_private_file(path: Path, *, allow_missing: bool = True) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    except OSError as exc:
        raise _security_error(f"private_file_error:{type(exc).__name__}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _security_error("private_file_not_regular")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise _security_error("private_file_wrong_owner")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise _security_error("private_file_permissions_must_be_0600")
    return info


def read_private_secret_file(path: Path, *, max_bytes: int = 4096) -> str | None:
    """Read an explicitly selected 0600 secret without following or racing links."""

    info = _validate_private_file(path)
    if info is None:
        return None
    if info.st_size > max_bytes:
        raise _security_error("private_secret_file_too_large")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (hasattr(os, "geteuid") and opened.st_uid != os.geteuid())
            or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
        ):
            raise _security_error("private_secret_file_changed_during_open")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1
            value = handle.read(max_bytes + 1)
    except GatewayError:
        raise
    except (OSError, UnicodeError) as exc:
        raise _security_error(f"private_secret_file_error:{type(exc).__name__}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if value.endswith("\r\n"):
        secret = value[:-2]
    elif value.endswith("\n"):
        secret = value[:-1]
    else:
        secret = value
    if (
        not secret
        or secret != secret.strip()
        or len(value.encode()) > max_bytes
        or contains_unsafe_text_codepoint(secret)
    ):
        raise _security_error("private_secret_file_invalid")
    return secret


@dataclass(frozen=True, slots=True)
class TokenRecord:
    version: int
    user_id: int
    username: str
    token: str = field(repr=False)
    saved_at: str
    proxy_url: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            username_bytes = len(self.username.encode("utf-8")) if isinstance(self.username, str) else 0
            token_bytes = len(self.token.encode("utf-8")) if isinstance(self.token, str) else 0
            saved_at_bytes = len(self.saved_at.encode("utf-8")) if isinstance(self.saved_at, str) else 0
        except UnicodeEncodeError as exc:
            raise _security_error("invalid_token_record_values") from exc
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version != 1
            or isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id <= 0
            or not isinstance(self.username, str)
            or not self.username
            or contains_unsafe_text_codepoint(self.username)
            or username_bytes > 1024
            or not isinstance(self.token, str)
            or not self.token
            or contains_unsafe_text_codepoint(self.token)
            or token_bytes > 16_384
            or not isinstance(self.saved_at, str)
            or not self.saved_at
            or contains_unsafe_text_codepoint(self.saved_at)
            or saved_at_bytes > 128
            or (self.proxy_url is not None and not isinstance(self.proxy_url, str))
        ):
            raise _security_error("invalid_token_record_values")
        if self.proxy_url is not None:
            try:
                normalized_proxy = normalize_proxy_url(self.proxy_url)
            except ValueError as exc:
                raise _security_error("invalid_token_record_values") from exc
            if not normalized_proxy or normalized_proxy != self.proxy_url:
                raise _security_error("invalid_token_record_values")
        try:
            saved_timestamp = datetime.fromisoformat(self.saved_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _security_error("invalid_token_record_values") from exc
        if saved_timestamp.tzinfo is None:
            raise _security_error("invalid_token_record_values")

    @classmethod
    def create(
        cls,
        *,
        user_id: int,
        username: str,
        token: str,
        proxy_url: str | None = None,
    ) -> TokenRecord:
        return cls(
            version=1,
            user_id=user_id,
            username=username,
            token=token,
            saved_at=datetime.now(UTC).isoformat(),
            proxy_url=proxy_url,
        )


class SecureTokenStore:
    """Account-scoped token files guarded by an interprocess flock."""

    def __init__(self, state_dir: Path, *, lock_timeout: float = 20.0) -> None:
        self._state_dir = ensure_secure_directory(state_dir)
        self._directory = ensure_secure_directory(self._state_dir / "tokens")
        self._lock_timeout = lock_timeout

    def _safe_name(self, scope: str) -> str:
        if not re.fullmatch(r"[a-z0-9-]{1,96}", scope):
            raise _security_error("invalid_token_scope")
        return scope

    def _token_path(self, scope: str) -> Path:
        return self._directory / f"{self._safe_name(scope)}.json"

    def _lock_path(self, scope: str) -> Path:
        return self._directory / f"{self._safe_name(scope)}.lock"

    def acquire_lock(self, scope: str) -> int:
        lock_path = self._lock_path(scope)
        open_flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        try:
            try:
                fd = os.open(
                    lock_path,
                    open_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                fd = os.open(lock_path, open_flags)
            else:
                os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise _security_error("token_lock_not_regular")
            if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                raise _security_error("token_lock_wrong_owner")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise _security_error("token_lock_permissions_must_be_0600")
            deadline = time.monotonic() + self._lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise GatewayError(
                            ErrorCode.AUTH_IN_PROGRESS,
                            retryable=True,
                            safe_to_retry=True,
                            retry_after_seconds=1.0,
                            diagnostic="token_store_lock_timeout",
                        ) from exc
                    time.sleep(0.05)
            return fd
        except GatewayError:
            if "fd" in locals():
                os.close(fd)
            raise
        except OSError as exc:
            if "fd" in locals():
                os.close(fd)
            raise _security_error(f"token_lock_error:{type(exc).__name__}") from exc

    @staticmethod
    def release_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def load_locked(self, scope: str) -> TokenRecord | None:
        path = self._token_path(scope)
        info = _validate_private_file(path)
        if info is None:
            return None
        if info.st_size > 65_536:
            raise _security_error("token_file_too_large")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = -1
        try:
            fd = os.open(path, flags)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (hasattr(os, "geteuid") and opened.st_uid != os.geteuid())
                or opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
            ):
                raise _security_error("token_file_changed_during_open")
            handle = os.fdopen(fd, encoding="utf-8")
            fd = -1
            with handle:
                payload = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise _security_error(f"invalid_token_file:{type(exc).__name__}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(payload, dict):
            raise _security_error("invalid_token_record_shape")
        legacy_shape = {
            "version",
            "user_id",
            "username",
            "token",
            "saved_at",
        }
        current_shape = legacy_shape | {"proxy_url"}
        if frozenset(payload) not in {frozenset(legacy_shape), frozenset(current_shape)}:
            raise _security_error("invalid_token_record_shape")
        version = payload["version"]
        user_id = payload["user_id"]
        username = payload["username"]
        token = payload["token"]
        saved_at = payload["saved_at"]
        proxy_url = payload.get("proxy_url")
        record = TokenRecord(
            version=version,
            user_id=user_id,
            username=username,
            token=token,
            saved_at=saved_at,
            proxy_url=proxy_url,
        )
        self._validate_scope_record(scope, record)
        return record

    def save_locked(self, scope: str, record: TokenRecord) -> None:
        self._validate_scope_record(scope, record)
        target = self._token_path(scope)
        _validate_private_file(target)
        payload = json.dumps(
            asdict(record),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(payload) > 65_536:
            raise _security_error("token_record_too_large")
        temp_fd = -1
        temp_name = ""
        replaced = False
        try:
            temp_fd, temp_name = tempfile.mkstemp(prefix=".token-", dir=self._directory)
            os.fchmod(temp_fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("short token write")
                view = view[written:]
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            os.replace(temp_name, target)
            replaced = True
            directory_fd = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            _validate_private_file(target, allow_missing=False)
        except GatewayError as exc:
            if replaced:
                raise GatewayError(
                    ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
                    reconciliation_required=True,
                    diagnostic="token_replace_commit_durability_unknown",
                ) from exc
            raise
        except OSError as exc:
            if replaced:
                raise GatewayError(
                    ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
                    reconciliation_required=True,
                    diagnostic=f"token_replace_commit_unknown:{type(exc).__name__}",
                ) from exc
            raise _security_error(f"token_write_error:{type(exc).__name__}") from exc
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            if temp_name:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temp_name)

    def _validate_scope_record(self, scope: str, record: TokenRecord) -> None:
        safe_scope = self._safe_name(scope)
        match = re.fullmatch(r"account-([1-9][0-9]*)", safe_scope)
        if match is not None and record.user_id != int(match.group(1)):
            raise GatewayError(
                ErrorCode.ACCOUNT_MISMATCH,
                diagnostic="token_record_scope_mismatch",
            )

    def delete_locked(self, scope: str) -> None:
        path = self._token_path(scope)
        if _validate_private_file(path) is None:
            return
        try:
            path.unlink()
            directory_fd = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise _security_error(f"token_delete_error:{type(exc).__name__}") from exc


def sanitize_external(
    value: Any,
    *,
    secrets: Sequence[str] = (),
    _depth: int = 0,
) -> JsonValue:
    """Convert remote values to JSON while redacting transport credentials."""

    if _depth > 30:
        return "<max-depth>"
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, float) and not math.isfinite(value):
        return "<non-finite>"
    if value is None or isinstance(value, int | float | bool):
        return value
    if isinstance(value, Mapping):
        clean: dict[str, JsonValue] = {}
        for raw_key, item in value.items():
            raw_key_text = str(raw_key)
            sensitive_key = any(part in raw_key_text.casefold() for part in _SENSITIVE_KEY_PARTS)
            key = redact_text(raw_key_text, secrets)
            if sensitive_key:
                clean[key] = "<redacted>"
            else:
                clean[key] = sanitize_external(
                    item,
                    secrets=secrets,
                    _depth=_depth + 1,
                )
        return clean
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return [
            sanitize_external(
                item,
                secrets=secrets,
                _depth=_depth + 1,
            )
            for item in value
        ]
    return redact_text(str(value), secrets)


def redact_text(value: str, secrets: Sequence[str] = ()) -> str:
    searchable_value = canonicalize_percent_escape_case(value)
    intervals: list[tuple[int, int]] = []
    for secret in sorted({secret for secret in secrets if secret}):
        searchable_secret = canonicalize_percent_escape_case(secret)
        offset = 0
        while True:
            start = searchable_value.find(searchable_secret, offset)
            if start < 0:
                break
            intervals.append((start, start + len(searchable_secret)))
            offset = start + 1

    redacted = value
    if intervals:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                previous_start, previous_end = merged[-1]
                merged[-1] = (previous_start, max(previous_end, end))
            else:
                merged.append((start, end))
        pieces: list[str] = []
        cursor = 0
        for start, end in merged:
            pieces.extend((value[cursor:start], "<redacted>"))
            cursor = end
        pieces.append(value[cursor:])
        redacted = "".join(pieces)

    def redact_url_userinfo(match: re.Match[str]) -> str:
        authority = match.group("authority")
        if "@" not in authority:
            return match.group(0)
        _userinfo, host = authority.rsplit("@", 1)
        return f"{match.group('scheme')}://<redacted>@{host}"

    return _URL_AUTHORITY.sub(redact_url_userinfo, redacted)


def configure_logging(config: KworkConfig) -> None:
    register_redaction_secrets(config.redaction_secrets)

    def patch(record: dict[str, Any]) -> None:
        record["message"] = redact_text(
            str(record["message"]),
            runtime_redaction_secrets(),
        )

    logger.remove()
    logger.configure(patcher=cast(Any, patch))
    logger.add(
        sys.stderr,
        level=config.log_level,
        format="{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | {level} | {message}",
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )
