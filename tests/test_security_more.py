from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from yarl import URL

import kwork_mcp.security as security_module
from kwork_mcp.config import KworkConfig, proxy_redaction_secrets
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode
from kwork_mcp.security import (
    SecureTokenStore,
    TokenRecord,
    cancellation_safe_fd_guard,
    configure_logging,
    ensure_secure_directory,
    redact_text,
    sanitize_external,
)


def _assert_validation(error: pytest.ExceptionInfo[GatewayError], diagnostic: str) -> None:
    assert error.value.code is ErrorCode.VALIDATION
    assert error.value.diagnostic == diagnostic


def _assert_no_secret_reflection(rendered: str, secrets: tuple[str, ...]) -> None:
    if any(secret and secret in rendered for secret in secrets):
        pytest.fail("redaction output contains a protected value", pytrace=False)


def test_secure_directory_creates_private_parents_and_rejects_regular_file(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "private" / "nested"
    ensure_secure_directory(nested)
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700

    regular = tmp_path / "regular"
    regular.write_text("not a directory")
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(regular)
    assert caught.value.diagnostic == "state_directory_chain_error:NotADirectoryError"


def test_secure_directory_supports_execute_only_trusted_ancestor(
    tmp_path: Path,
) -> None:
    searchable = tmp_path / "searchable"
    searchable.mkdir(mode=0o700)
    os.chmod(searchable, 0o711)
    state = searchable / "state"

    assert ensure_secure_directory(state) == state
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_secure_directory_creation_is_private_under_restrictive_umask(
    tmp_path: Path,
) -> None:
    old_umask = os.umask(0o777)
    try:
        state = tmp_path / "umask-state"
        assert ensure_secure_directory(state) == state
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_secure_directory_wraps_creation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_mkdir(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(security_module.os, "mkdir", fail_mkdir)
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(tmp_path / "blocked")
    _assert_validation(caught, "state_directory_chain_error:PermissionError")


def test_secure_directory_rejects_real_nonsticky_writable_ancestor(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    os.chmod(shared, 0o777)
    private = shared / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)

    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(private)

    _assert_validation(
        caught,
        "state_directory_untrusted_writable_ancestor",
    )


def test_secure_directory_allows_one_sticky_temp_boundary_but_not_nested_shared(
    tmp_path: Path,
) -> None:
    sticky = tmp_path / "sticky"
    sticky.mkdir(mode=0o700)
    os.chmod(sticky, 0o1777)
    private = sticky / "private"
    assert ensure_secure_directory(private) == private.resolve()
    assert stat.S_IMODE(private.stat().st_mode) == 0o700

    nested_shared = sticky / "nested-shared"
    nested_shared.mkdir(mode=0o700)
    os.chmod(nested_shared, 0o1777)
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(nested_shared / "private")
    _assert_validation(
        caught,
        "state_directory_nested_shared_writable_ancestor",
    )


@pytest.mark.parametrize("unsafe_mode", [0o710, 0o1700, 0o2700, 0o4700])
def test_secure_directory_requires_exact_final_mode(
    tmp_path: Path,
    unsafe_mode: int,
) -> None:
    state = tmp_path / f"state-{unsafe_mode:o}"
    state.mkdir(mode=0o700)
    os.chmod(state, unsafe_mode)
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(state)
    _assert_validation(caught, "state_directory_permissions_must_be_0700")


def test_secure_directory_resolves_only_trusted_nonfinal_aliases(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    canonical = ensure_secure_directory(alias / "state")
    assert canonical == target / "state"
    assert canonical.is_dir()

    sticky = tmp_path / "sticky-alias-parent"
    sticky.mkdir(mode=0o700)
    os.chmod(sticky, 0o1777)
    unsafe_alias = sticky / "alias"
    unsafe_alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(unsafe_alias / "state")
    _assert_validation(caught, "state_directory_untrusted_symlink_ancestor")


def test_secure_directory_checks_every_alias_hop_in_target_chain(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    sticky = tmp_path / "sticky-hop"
    sticky.mkdir(mode=0o700)
    os.chmod(sticky, 0o1777)
    relay = sticky / "relay"
    relay.symlink_to(target, target_is_directory=True)
    trusted_alias = tmp_path / "trusted-alias"
    trusted_alias.symlink_to(relay, target_is_directory=True)

    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(trusted_alias / "state")

    _assert_validation(caught, "state_directory_untrusted_symlink_ancestor")
    assert not (target / "state").exists()


def test_secure_directory_resolves_relative_alias_and_rejects_alias_loop(
    tmp_path: Path,
) -> None:
    target = tmp_path / "relative-target"
    target.mkdir(mode=0o700)
    relative_alias = tmp_path / "relative-alias"
    relative_alias.symlink_to("relative-target", target_is_directory=True)
    assert ensure_secure_directory(relative_alias / "state") == target / "state"

    first = tmp_path / "loop-first"
    second = tmp_path / "loop-second"
    first.symlink_to("loop-second", target_is_directory=True)
    second.symlink_to("loop-first", target_is_directory=True)
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(first / "state")
    _assert_validation(caught, "state_directory_alias_loop")


def test_secure_directory_wraps_readlink_failure_without_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "readlink-target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "readlink-alias"
    alias.symlink_to(target, target_is_directory=True)

    def fail_readlink(_path: Path) -> str:
        raise PermissionError("blocked")

    monkeypatch.setattr(security_module.os, "readlink", fail_readlink)
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(alias / "state")
    _assert_validation(
        caught,
        "state_directory_alias_resolution_error:PermissionError",
    )
    assert not (target / "state").exists()


def test_secure_directory_symlink_injection_race_is_never_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_parent = tmp_path / "safe"
    safe_parent.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    real_mkdir = security_module.os.mkdir

    def inject_symlink(
        path: str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == "raced":
            os.symlink(outside, path, dir_fd=dir_fd)
            raise FileExistsError
        real_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security_module.os, "mkdir", inject_symlink)
    with pytest.raises(GatewayError):
        ensure_secure_directory(safe_parent / "raced" / "state")
    assert list(outside.iterdir()) == []


def test_secure_directory_never_weakly_resolves_missing_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_resolve = Path.resolve

    def reject_weak_resolution(
        self: Path,
        strict: bool = False,
    ) -> Path:
        if not strict:
            pytest.fail("missing path components must be opened with O_NOFOLLOW")
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", reject_weak_resolution)
    target = tmp_path / "missing" / "state"
    assert ensure_secure_directory(target) == target
    assert target.is_dir()


@pytest.mark.parametrize(
    "unsafe",
    [
        Path("/tmp/../tmp/kwork-mcp-state"),
        Path("//tmp/kwork-mcp-state"),
        Path("/tmp/kwork\nmcp-state"),
    ],
)
def test_secure_directory_rejects_ambiguous_lexical_paths(unsafe: Path) -> None:
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(unsafe)
    _assert_validation(caught, "state_directory_path_invalid")


def test_token_store_rejects_invalid_scope_and_non_regular_lock(tmp_path: Path) -> None:
    store = SecureTokenStore(tmp_path / "state")
    for scope in ("../escape", "UPPERCASE", "", "a" * 97):
        with pytest.raises(GatewayError) as caught:
            store.load_locked(scope)
        _assert_validation(caught, "invalid_token_scope")

    lock = tmp_path / "state" / "tokens" / "account-1.lock"
    lock.mkdir(mode=0o700)
    with pytest.raises(GatewayError) as caught:
        store.acquire_lock("account-1")
    _assert_validation(caught, "token_lock_error:IsADirectoryError")


def test_fresh_token_lock_is_private_under_restrictive_umask(tmp_path: Path) -> None:
    old_umask = os.umask(0o777)
    fd: int | None = None
    store: SecureTokenStore | None = None
    try:
        store = SecureTokenStore(tmp_path / "token-lock-umask-state")
        fd = store.acquire_lock("account-1")
    finally:
        if fd is not None and store is not None:
            store.release_lock(fd)
        os.umask(old_umask)

    lock = tmp_path / "token-lock-umask-state" / "tokens" / "account-1.lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_existing_token_lock_permissions_are_rejected_without_repair(
    tmp_path: Path,
) -> None:
    store = SecureTokenStore(tmp_path / "state")
    lock = tmp_path / "state" / "tokens" / "account-1.lock"
    lock.touch(mode=0o644)
    os.chmod(lock, 0o644)

    with pytest.raises(GatewayError) as caught:
        store.acquire_lock("account-1")

    _assert_validation(caught, "token_lock_permissions_must_be_0600")
    assert stat.S_IMODE(lock.stat().st_mode) == 0o644


def test_token_store_lock_timeout_is_bounded(tmp_path: Path) -> None:
    store = SecureTokenStore(tmp_path / "state", lock_timeout=0.01)
    first = store.acquire_lock("account-1")
    try:
        contender = SecureTokenStore(tmp_path / "state", lock_timeout=0.01)
        with pytest.raises(GatewayError) as caught:
            contender.acquire_lock("account-1")
        assert caught.value.code is ErrorCode.AUTH_IN_PROGRESS
        assert caught.value.retry_after_seconds == 1.0
    finally:
        store.release_lock(first)


@pytest.mark.asyncio
async def test_fd_guard_releases_then_reraises_cancellation_during_release() -> None:
    release_started = threading.Event()
    release_allowed = threading.Event()
    released: list[int] = []

    def release(fd: int) -> None:
        release_started.set()
        if not release_allowed.wait(timeout=2):
            raise TimeoutError("test release was not unblocked")
        released.append(fd)

    async def guarded_operation() -> None:
        async with cancellation_safe_fd_guard(lambda: 17, release):
            pass

    task = asyncio.create_task(guarded_operation())
    assert await asyncio.to_thread(release_started.wait, 2)
    task.cancel()
    release_allowed.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert released == [17]


@pytest.mark.asyncio
async def test_fd_guard_preserves_body_cancellation_when_release_also_fails() -> None:
    entered = asyncio.Event()

    def release(_fd: int) -> None:
        raise RuntimeError("release-failed")

    async def guarded_operation() -> None:
        async with cancellation_safe_fd_guard(lambda: 17, release):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(guarded_operation())
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_fd_guard_preserves_typed_primary_when_release_fails_after_unlock() -> None:
    from loguru import logger

    locked = True
    log_messages: list[str] = []
    protected_value = "synthetic-release-detail"
    sink_id = logger.add(
        lambda message: log_messages.append(str(message)),
        format="{message}",
    )

    def release(_fd: int) -> None:
        nonlocal locked
        locked = False
        raise RuntimeError(protected_value)

    try:
        with pytest.raises(GatewayError) as caught:
            async with cancellation_safe_fd_guard(lambda: 17, release):
                raise GatewayError(
                    ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
                    reconciliation_required=True,
                    diagnostic="synthetic_credential_update_unknown",
                )
    finally:
        logger.remove(sink_id)

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    assert locked is False
    assert protected_value not in "".join(log_messages)


@pytest.mark.asyncio
async def test_fd_guard_preserves_ordinary_primary_when_release_fails_before_unlock() -> None:
    locked = True
    primary = ValueError("ordinary-primary")

    def release(_fd: int) -> None:
        raise RuntimeError("release-failed-before-unlock")

    with pytest.raises(ValueError) as caught:
        async with cancellation_safe_fd_guard(lambda: 17, release):
            raise primary

    assert caught.value is primary
    assert locked is True


@pytest.mark.asyncio
async def test_fd_guard_release_failure_without_primary_is_fail_loud() -> None:
    locked = True
    release_error = RuntimeError("release-failed-after-unlock")

    def release(_fd: int) -> None:
        nonlocal locked
        locked = False
        raise release_error

    with pytest.raises(RuntimeError) as caught:
        async with cancellation_safe_fd_guard(lambda: 17, release):
            pass

    assert caught.value is release_error
    assert locked is False


@pytest.mark.asyncio
async def test_fd_guard_release_failure_wins_over_release_cancellation_without_primary() -> None:
    release_started = threading.Event()
    release_allowed = threading.Event()

    def release(_fd: int) -> None:
        release_started.set()
        if not release_allowed.wait(timeout=2):
            raise TimeoutError("test release was not unblocked")
        raise RuntimeError("release-failed")

    async def guarded_operation() -> None:
        async with cancellation_safe_fd_guard(lambda: 17, release):
            pass

    task = asyncio.create_task(guarded_operation())
    assert await asyncio.to_thread(release_started.wait, 2)
    task.cancel()
    release_allowed.set()

    with pytest.raises(RuntimeError, match="release-failed"):
        await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_fd_guard_release_timeout_is_bounded_and_preserves_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security_module, "_FD_RELEASE_TIMEOUT_SECONDS", 0.02)
    release_allowed = threading.Event()
    release_finished = threading.Event()

    def release(_fd: int) -> None:
        release_allowed.wait(timeout=2)
        release_finished.set()

    try:
        with pytest.raises(GatewayError) as caught:
            await asyncio.wait_for(
                _raise_typed_error_inside_fd_guard(release),
                timeout=0.5,
            )
        assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    finally:
        release_allowed.set()
        assert await asyncio.to_thread(release_finished.wait, 0.5)


async def _raise_typed_error_inside_fd_guard(
    release: Any,
) -> None:
    async with cancellation_safe_fd_guard(lambda: 17, release):
        raise GatewayError(
            ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
            reconciliation_required=True,
            diagnostic="synthetic_credential_update_unknown",
        )


@pytest.mark.asyncio
async def test_fd_guard_does_not_mask_active_typed_error_with_release_cancellation() -> None:
    release_started = threading.Event()
    release_allowed = threading.Event()
    released: list[int] = []

    def release(fd: int) -> None:
        release_started.set()
        if not release_allowed.wait(timeout=2):
            raise TimeoutError("test release was not unblocked")
        released.append(fd)

    async def guarded_operation() -> None:
        async with cancellation_safe_fd_guard(lambda: 17, release):
            raise GatewayError(
                ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
                reconciliation_required=True,
                diagnostic="synthetic_credential_update_unknown",
            )

    task = asyncio.create_task(guarded_operation())
    assert await asyncio.to_thread(release_started.wait, 2)
    task.cancel()
    release_allowed.set()

    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(task, timeout=0.5)
    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    assert released == [17]


@pytest.mark.asyncio
async def test_shield_helper_propagates_child_task_cancellation_without_spinning() -> None:
    async def cancelled_child() -> None:
        raise asyncio.CancelledError

    child = asyncio.create_task(cancelled_child())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(security_module._finish_task(child), timeout=0.5)


@pytest.mark.parametrize(
    ("payload", "diagnostic"),
    [
        ("{", "invalid_token_file:JSONDecodeError"),
        ("[]", "invalid_token_record_shape"),
        (
            json.dumps(
                {
                    "version": 1,
                    "user_id": 42,
                    "username": "u",
                    "token": "t",
                    "saved_at": "now",
                    "extra": True,
                }
            ),
            "invalid_token_record_shape",
        ),
        (
            json.dumps(
                {
                    "version": "bad",
                    "user_id": 42,
                    "username": "u",
                    "token": "t",
                    "saved_at": "now",
                }
            ),
            "invalid_token_record_values",
        ),
        (
            json.dumps(
                {
                    "version": 2,
                    "user_id": 0,
                    "username": "",
                    "token": "",
                    "saved_at": "now",
                }
            ),
            "invalid_token_record_values",
        ),
        (
            json.dumps(
                {
                    "version": "1",
                    "user_id": True,
                    "username": None,
                    "token": False,
                    "saved_at": None,
                }
            ),
            "invalid_token_record_values",
        ),
    ],
)
def test_token_store_rejects_malformed_records(
    tmp_path: Path,
    payload: str,
    diagnostic: str,
) -> None:
    store = SecureTokenStore(tmp_path / "state")
    token_path = tmp_path / "state" / "tokens" / "account-42.json"
    token_path.write_text(payload)
    os.chmod(token_path, 0o600)
    with pytest.raises(GatewayError) as caught:
        store.load_locked("account-42")
    _assert_validation(caught, diagnostic)


def test_token_store_rejects_oversized_and_changed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SecureTokenStore(tmp_path / "state")
    token_path = tmp_path / "state" / "tokens" / "account-42.json"
    token_path.write_bytes(b"x" * 65_537)
    os.chmod(token_path, 0o600)
    with pytest.raises(GatewayError) as oversized:
        store.load_locked("account-42")
    _assert_validation(oversized, "token_file_too_large")

    record = TokenRecord.create(user_id=42, username="u", token="t")
    token_path.write_text(json.dumps(record.__dict__) if hasattr(record, "__dict__") else "{}")
    # Slots have no __dict__; write the exact wire shape explicitly.
    token_path.write_text(
        json.dumps(
            {
                "version": record.version,
                "user_id": record.user_id,
                "username": record.username,
                "token": record.token,
                "saved_at": record.saved_at,
            }
        )
    )
    os.chmod(token_path, 0o600)
    real_fstat = os.fstat

    def changed_inode(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        values = list(info)
        values[1] += 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", changed_inode)
    with pytest.raises(GatewayError) as changed:
        store.load_locked("account-42")
    _assert_validation(changed, "token_file_changed_during_open")


def test_token_store_wraps_short_write_and_cleans_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SecureTokenStore(tmp_path / "state")
    monkeypatch.setattr(os, "write", lambda _fd, _view: 0)
    with pytest.raises(GatewayError) as caught:
        store.save_locked(
            "account-42",
            TokenRecord.create(user_id=42, username="u", token="t"),
        )
    assert caught.value.diagnostic == "token_write_error:OSError"
    assert list((tmp_path / "state" / "tokens").glob(".token-*")) == []


def test_token_store_delete_missing_and_wraps_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SecureTokenStore(tmp_path / "state")
    store.delete_locked("account-42")
    record = TokenRecord.create(user_id=42, username="u", token="t")
    store.save_locked("account-42", record)

    def fail_unlink(_self: Path, *, missing_ok: bool = False) -> None:
        del missing_ok
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(GatewayError) as caught:
        store.delete_locked("account-42")
    assert caught.value.diagnostic == "token_delete_error:PermissionError"


class FixtureModel(BaseModel):
    value: str


@dataclass
class FixtureDataclass:
    value: str


class TextObject:
    def __str__(self) -> str:
        return "object-secret"


def test_sanitize_external_handles_models_dataclasses_sequences_depth_and_fallback() -> None:
    assert sanitize_external(FixtureModel(value="secret"), secrets=("secret",)) == {"value": "<redacted>"}
    assert sanitize_external(FixtureDataclass(value="safe")) == {"value": "safe"}
    assert sanitize_external((1, b"bytes", TextObject()), secrets=("secret",)) == [
        1,
        "b'bytes'",
        "object-<redacted>",
    ]

    nested: dict[str, Any] = {}
    current = nested
    for _ in range(32):
        child: dict[str, Any] = {}
        current["next"] = child
        current = child
    sanitized = sanitize_external(nested)
    cursor: Any = sanitized
    for _ in range(30):
        cursor = cursor["next"]
    assert cursor["next"] == "<max-depth>"
    assert sanitize_external([math.nan, math.inf, -math.inf]) == [
        "<non-finite>",
        "<non-finite>",
        "<non-finite>",
    ]
    assert sanitize_external(
        {"https://alice:secret@proxy.example/fixture-token": "safe"},
        secrets=("fixture-token",),
    ) == {"https://<redacted>@proxy.example/<redacted>": "<redacted>"}


def test_redaction_checks_raw_sensitive_keys_and_longest_secret_first() -> None:
    short = "overlap-secret"
    long = "overlap-secret-extended"
    sanitized = sanitize_external(
        {
            "token": long,
            "safe": f"{long} {short}",
        },
        secrets=("token", short, long),
    )
    assert sanitized == {
        "<redacted>": "<redacted>",
        "safe": "<redacted> <redacted>",
    }
    rendered = redact_text(f"{long} {short}", (short, long))
    assert rendered == "<redacted> <redacted>"
    assert "-extended" not in rendered


def test_redaction_merges_cross_offset_and_equal_length_secret_overlaps() -> None:
    cross_offset = redact_text("A1BCDE", ("A1", "1BCDE"))
    equal_length = redact_text("XYZ12", ("XYZ1", "YZ12"))
    touching = redact_text("left-right", ("left-", "right"))

    _assert_no_secret_reflection(
        cross_offset,
        ("A1", "1BCDE", "BCDE"),
    )
    _assert_no_secret_reflection(
        equal_length,
        ("XYZ1", "YZ12", "XYZ12"),
    )
    _assert_no_secret_reflection(
        touching,
        ("left-", "right", "left-right"),
    )
    if (cross_offset, equal_length, touching) != (
        "<redacted>",
        "<redacted>",
        "<redacted>",
    ):
        pytest.fail("overlapping exact-secret intervals were not fully merged", pytrace=False)


def test_proxy_redaction_handles_raw_at_percent_encoding_and_external_mappings() -> None:
    raw_proxy = "http://proxy-user:p@ssword@proxy.example:8080"
    encoded_proxy = "socks5://encoded-user:p%2f%3aword@proxy.example:1080"
    mixed_proxy = "socks5://encoded-user:p%2F%3aword@proxy.example:1080"
    secrets = proxy_redaction_secrets(raw_proxy) + proxy_redaction_secrets(encoded_proxy)
    reflected = " | ".join(
        (
            raw_proxy,
            encoded_proxy,
            mixed_proxy,
            "proxy-user:p@ssword",
            "p@ssword",
            "encoded-user:p%2F%3aword",
            "p%2F%3aword",
            "encoded-user:p/:word",
            "p/:word",
        )
    )

    generic_raw = redact_text(f"upstream={raw_proxy}")
    generic_encoded = redact_text(f"upstream={encoded_proxy}")
    exact = redact_text(reflected, secrets)
    sanitized = sanitize_external(
        {
            raw_proxy: reflected,
            "nested": [
                {encoded_proxy: "encoded-user:p%2f%3Aword"},
                reflected,
            ],
        },
        secrets=secrets,
    )
    rendered_mapping = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)

    generic_protected = (
        "proxy-user:p@ssword",
        "p@ssword",
        "encoded-user:p%2f%3aword",
        "p%2f%3aword",
    )
    exact_protected = tuple(
        dict.fromkeys(
            (
                *secrets,
                mixed_proxy,
                "p@ssword",
                "p%2F%3aword",
                "p/:word",
            )
        )
    )
    for rendered in (generic_raw, generic_encoded):
        _assert_no_secret_reflection(rendered, generic_protected)
    for rendered in (exact, rendered_mapping):
        _assert_no_secret_reflection(rendered, exact_protected)
    assert generic_raw == "upstream=http://<redacted>@proxy.example:8080"
    assert generic_encoded == "upstream=socks5://<redacted>@proxy.example:1080"
    assert exact.count("<redacted>") >= 4
    assert "<redacted>" in rendered_mapping


@pytest.mark.parametrize(
    "proxy",
    [
        "http://percent-user:p%2fword@proxy.example:8080",
        "http://percent-user:p%2Fword@proxy.example:8080",
        "HTTPS://percent-user:p%2fword@PROXY.EXAMPLE:443",
        "HTTP://PROXY.EXAMPLE:80",
    ],
)
def test_proxy_redaction_covers_yarl_percent_escape_normalization(proxy: str) -> None:
    parsed = URL(proxy)
    derived = tuple(
        value
        for value in (
            str(parsed),
            parsed.raw_authority,
            parsed.raw_host,
            parsed.host,
            parsed.raw_user,
            parsed.raw_password,
            proxy.replace("%2f", "%2F"),
            proxy.replace("%2F", "%2f"),
        )
        if value
    )
    secrets = proxy_redaction_secrets(proxy)
    rendered = " ".join(redact_text(value, secrets) for value in derived)

    _assert_no_secret_reflection(
        rendered,
        tuple(dict.fromkeys((*derived, "p/word"))),
    )
    if "<redacted>" not in rendered:
        pytest.fail("canonical proxy credential variants were not redacted", pytrace=False)


def test_proxy_redaction_canonicalizes_each_percent_escape_independently() -> None:
    proxy = "http://mixed-user:p%2f%3aword@proxy.example:8080"
    reflected = (
        "http://mixed-user:p%2F%3aword@proxy.example:8080",
        "http://mixed-user:p%2f%3Aword@proxy.example:8080",
        "mixed-user:p%2F%3aword",
        "p%2f%3Aword",
    )
    secrets = proxy_redaction_secrets(proxy)
    rendered = " ".join(redact_text(value, secrets) for value in reflected)

    _assert_no_secret_reflection(rendered, reflected)
    if "<redacted>" not in rendered:
        pytest.fail("mixed-case percent escapes were not redacted", pytrace=False)


def test_configure_logging_redacts_encoded_and_decoded_proxy_credentials(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proxy = "socks5://encoded-user:p%2f%3aword@proxy.example:1080"
    config = KworkConfig(
        token="proxy-log-token",
        proxy_url=proxy,
        state_dir=tmp_path,
    )
    configure_logging(config)
    from loguru import logger

    logger.info(
        "proxy={} decoded={} userinfo={} password={}",
        proxy,
        "socks5://encoded-user:p/:word@proxy.example:1080",
        "encoded-user:p%2F%3aword",
        "p%2f%3Aword",
    )
    output = capsys.readouterr().err
    protected = tuple(
        dict.fromkeys(
            (
                *proxy_redaction_secrets(proxy),
                "socks5://encoded-user:p/:word@proxy.example:1080",
                "encoded-user:p%2F%3aword",
                "p%2f%3Aword",
                "p/:word",
            )
        )
    )
    _assert_no_secret_reflection(output, protected)
    assert "<redacted>" in output
    logger.remove()
    logger.configure(patcher=None)
    logger.add(sys.__stderr__)


def test_configure_logging_redacts_configured_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = KworkConfig(
        token="overlap-secret-extended",
        login="overlap-secret",
        password="pass-word",
        proxy_url="socks5://alice:proxy-pass@localhost:1080",
        state_dir=tmp_path,
    )
    configure_logging(config)
    from loguru import logger

    logger.info(
        "token={} login={} password={} proxy={}",
        "overlap-secret-extended",
        "overlap-secret",
        "pass-word",
        "socks5://alice:proxy-pass@localhost:1080",
    )
    output = capsys.readouterr().err
    assert "overlap-secret-extended" not in output
    assert "overlap-secret" not in output
    assert "-extended" not in output
    assert "pass-word" not in output
    assert "proxy-pass" not in output
    assert "<redacted>" in output
    logger.remove()
    logger.configure(patcher=None)
    logger.add(sys.__stderr__)
