from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest
from kwork.exceptions import KworkHTTPException
from kwork.schema.actor import Actor
from pydantic import ValidationError

import kwork_mcp.bootstrap as bootstrap_module
import kwork_mcp.security as security_module
from kwork_mcp.bootstrap import bootstrap_account, run_bootstrap_cli
from kwork_mcp.config import KworkConfig, secret_server_environment_present
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode
from kwork_mcp.security import (
    SecureTokenStore,
    TokenRecord,
    read_private_secret_file,
)
from kwork_mcp.session import KworkSessionManager


class AuthClient:
    def __init__(
        self,
        actor: Actor | BaseException,
        *,
        fresh_token: str = "fresh-token",
    ) -> None:
        self.actor = actor
        self.fresh_token = fresh_token
        self._token: str | None = None
        self.get_me_calls = 0
        self.get_token_calls = 0
        self.closed = 0

    async def get_me(self) -> Actor:
        self.get_me_calls += 1
        if isinstance(self.actor, BaseException):
            raise self.actor
        return self.actor

    async def get_token(self) -> str:
        self.get_token_calls += 1
        self._token = self.fresh_token
        return self.fresh_token

    async def close(self) -> None:
        self.closed += 1


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class BlockingAuthClient(AuthClient):
    def __init__(
        self,
        actor: Actor,
        *,
        entered: asyncio.Event,
        release: asyncio.Event,
        fresh_token: str = "fresh-token",
    ) -> None:
        super().__init__(actor, fresh_token=fresh_token)
        self._entered = entered
        self._release = release

    async def get_me(self) -> Actor:
        self._entered.set()
        await self._release.wait()
        return await super().get_me()


class BlockingCloseAuthClient(AuthClient):
    def __init__(
        self,
        actor: Actor,
        *,
        close_entered: asyncio.Event,
        close_release: asyncio.Event,
        fresh_token: str = "fresh-token",
    ) -> None:
        super().__init__(actor, fresh_token=fresh_token)
        self._close_entered = close_entered
        self._close_release = close_release

    async def close(self) -> None:
        self._close_entered.set()
        await self._close_release.wait()
        await super().close()


class SelfCancellingCloseAuthClient(AuthClient):
    async def close(self) -> None:
        self.closed += 1
        raise asyncio.CancelledError


class CancellationResistantCloseAuthClient(AuthClient):
    def __init__(
        self,
        actor: Actor,
        *,
        close_entered: asyncio.Event,
        close_release: asyncio.Event,
        close_finished: asyncio.Event,
        fresh_token: str = "fresh-token",
    ) -> None:
        super().__init__(actor, fresh_token=fresh_token)
        self._close_entered = close_entered
        self._close_release = close_release
        self._close_finished = close_finished

    async def close(self) -> None:
        self._close_entered.set()
        while not self._close_release.is_set():
            try:
                await self._close_release.wait()
            except asyncio.CancelledError:
                continue
        self.closed += 1
        self._close_finished.set()


class BlockingReleaseTokenStore(SecureTokenStore):
    def __init__(
        self,
        state_dir: Path,
        *,
        release_started: threading.Event,
        release_allowed: threading.Event,
    ) -> None:
        super().__init__(state_dir)
        self._release_started = release_started
        self._release_allowed = release_allowed

    def release_lock(self, fd: int) -> None:
        self._release_started.set()
        if not self._release_allowed.wait(timeout=2):
            raise TimeoutError("test token lock release was not unblocked")
        super().release_lock(fd)


class RaisingAfterReleaseTokenStore(SecureTokenStore):
    def __init__(self, state_dir: Path, *, protected_detail: str) -> None:
        super().__init__(state_dir)
        self._protected_detail = protected_detail

    def release_lock(self, fd: int) -> None:
        super().release_lock(fd)
        raise RuntimeError(self._protected_detail)


class RecordingBootstrapCoordinator(CoordinationStore):
    def __init__(self, config: KworkConfig) -> None:
        super().__init__(config)
        self.acquired: list[tuple[str, str]] = []
        self.successes: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str, float | None]] = []

    async def acquire(self, scope: str, route: str) -> None:
        self.acquired.append((scope, route))

    async def record_success(self, scope: str, route: str) -> None:
        self.successes.append((scope, route))

    async def record_failure(
        self,
        scope: str,
        route: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.failures.append((scope, route, retry_after_seconds))


def unauthorized() -> KworkHTTPException:
    return KworkHTTPException(
        "opaque",
        status=401,
        response_json={"success": False},
    )


def credentialless_config(state_dir: Path, **overrides: Any) -> KworkConfig:
    values: dict[str, Any] = {
        "token": None,
        "login": "",
        "password": "",
        "expected_user_id": 42,
        "persist_token": True,
        "state_dir": state_dir,
        "rps_limit": 100.0,
        "burst_limit": 100,
        "route_rps_limit": 100.0,
        "route_burst_limit": 100,
        "retry_backoff_base": 0.0,
        "retry_backoff_max": 0.0,
    }
    values.update(overrides)
    return KworkConfig(**values)


def save_record(store: SecureTokenStore, record: TokenRecord) -> None:
    scope = f"account-{record.user_id}"
    fd = store.acquire_lock(scope)
    try:
        store.save_locked(scope, record)
    finally:
        store.release_lock(fd)


def load_record(store: SecureTokenStore, scope: str = "account-42") -> TokenRecord | None:
    fd = store.acquire_lock(scope)
    try:
        return store.load_locked(scope)
    finally:
        store.release_lock(fd)


def test_credentialless_config_requires_stable_persisted_scope(tmp_path: Path) -> None:
    valid = credentialless_config(tmp_path / "valid")
    assert valid.bootstrap_scope == "account-42"
    assert valid.token_cache_is_bound is True
    assert valid.fresh_credentials_available is False

    for overrides in (
        {"expected_user_id": None},
        {"persist_token": False},
    ):
        with pytest.raises(ValidationError, match="credentialless startup"):
            credentialless_config(tmp_path / f"invalid-{len(overrides)}", **overrides)


@pytest.mark.asyncio
async def test_credentialless_startup_uses_verified_store_without_fresh_login(
    tmp_path: Path,
) -> None:
    config = credentialless_config(tmp_path / "state")
    store = SecureTokenStore(config.state_dir)
    saved = TokenRecord.create(user_id=42, username="fixture", token="cached-token")
    save_record(store, saved)
    client = AuthClient(Actor(id=42, username="fixture"))
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: client,  # type: ignore[arg-type]
        token_store=store,
    )

    assert await session.ensure_client() is client
    assert session.scope == "account-42"
    assert client._token == "cached-token"
    assert client.get_me_calls == 1
    assert client.get_token_calls == 0
    assert load_record(store) == saved


@pytest.mark.asyncio
async def test_credentialless_missing_store_is_typed_auth_required_without_client(
    tmp_path: Path,
) -> None:
    config = credentialless_config(tmp_path / "state")
    factory_calls = 0

    def factory(_: KworkConfig) -> AuthClient:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("missing store must not create an upstream client")

    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=factory,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as caught:
        await session.ensure_client()
    assert caught.value.code is ErrorCode.AUTH_REQUIRED
    assert caught.value.diagnostic == "stored_token_missing"
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_rejected_store_is_not_retried_and_new_peer_token_is_adopted(
    tmp_path: Path,
) -> None:
    config = credentialless_config(tmp_path / "state")
    store = SecureTokenStore(config.state_dir)
    expired_record = TokenRecord.create(
        user_id=42,
        username="fixture",
        token="expired-token",
    )
    save_record(store, expired_record)
    expired = AuthClient(unauthorized())
    replacement = AuthClient(Actor(id=42, username="fixture"))
    clients = iter((expired, replacement))
    factory_calls = 0

    def factory(_: KworkConfig) -> AuthClient:
        nonlocal factory_calls
        factory_calls += 1
        return next(clients)

    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=factory,  # type: ignore[arg-type]
        token_store=store,
    )
    with pytest.raises(GatewayError) as first:
        await session.ensure_client()
    assert first.value.code is ErrorCode.AUTH_EXPIRED
    assert load_record(store) == expired_record
    with pytest.raises(GatewayError) as second:
        await session.ensure_client()
    assert second.value.code is ErrorCode.AUTH_EXPIRED
    assert factory_calls == 1
    assert expired.get_me_calls == 1

    save_record(
        store,
        TokenRecord.create(user_id=42, username="fixture", token="peer-token"),
    )
    assert await session.ensure_client() is replacement
    assert replacement._token == "peer-token"
    assert factory_calls == 2


@pytest.mark.asyncio
async def test_scope_record_mismatch_is_rejected_before_network(tmp_path: Path) -> None:
    config = credentialless_config(tmp_path / "state")
    store = SecureTokenStore(config.state_dir)
    token_path = config.state_dir / "tokens" / "account-42.json"
    token_path.write_text(
        json.dumps(
            {
                "version": 1,
                "user_id": 99,
                "username": "wrong",
                "token": "wrong-token",
                "saved_at": "2026-07-27T00:00:00+00:00",
            }
        )
    )
    os.chmod(token_path, 0o600)
    factory_calls = 0

    def factory(_: KworkConfig) -> AuthClient:
        nonlocal factory_calls
        factory_calls += 1
        return AuthClient(Actor(id=99, username="wrong"))

    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=factory,  # type: ignore[arg-type]
        token_store=store,
    )
    with pytest.raises(GatewayError) as caught:
        await session.ensure_client()
    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert factory_calls == 0
    assert token_path.exists()


@pytest.mark.asyncio
async def test_wrong_account_stored_token_preserves_record_byte_for_byte(
    tmp_path: Path,
) -> None:
    config = credentialless_config(tmp_path / "state")
    store = SecureTokenStore(config.state_dir)
    record = TokenRecord.create(
        user_id=42,
        username="expected",
        token="wrong-account-token",
    )
    save_record(store, record)
    path = config.state_dir / "tokens" / "account-42.json"
    before = path.read_bytes()
    client = AuthClient(Actor(id=99, username="other"))
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: client,  # type: ignore[arg-type]
        token_store=store,
    )

    with pytest.raises(GatewayError) as caught:
        await session.ensure_client()
    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert path.read_bytes() == before
    assert client.get_token_calls == 0


@pytest.mark.asyncio
async def test_stored_username_is_refreshable_metadata_not_primary_identity(
    tmp_path: Path,
) -> None:
    config = credentialless_config(tmp_path / "state")
    store = SecureTokenStore(config.state_dir)
    save_record(
        store,
        TokenRecord.create(user_id=42, username="old-name", token="cached"),
    )
    client = AuthClient(Actor(id=42, username="new-name"))
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: client,  # type: ignore[arg-type]
        token_store=store,
    )

    await session.ensure_client()
    refreshed = load_record(store)
    assert refreshed is not None
    assert refreshed.user_id == 42
    assert refreshed.username == "new-name"
    assert refreshed.token == "cached"


@pytest.mark.asyncio
async def test_relogin_without_credentials_rejects_once_and_clears_session(
    tmp_path: Path,
) -> None:
    config = credentialless_config(tmp_path / "state")
    store = SecureTokenStore(config.state_dir)
    stored = TokenRecord.create(
        user_id=42,
        username="fixture",
        token="cached",
    )
    save_record(store, stored)
    client = AuthClient(Actor(id=42, username="fixture"))
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: client,  # type: ignore[arg-type]
        token_store=store,
    )
    await session.ensure_client()

    with pytest.raises(GatewayError) as caught:
        await session.relogin(stale_client=client)  # type: ignore[arg-type]
    assert caught.value.code is ErrorCode.AUTH_EXPIRED
    assert session.actor is None
    assert client.closed == 1
    assert load_record(store) == stored
    assert client.get_token_calls == 0


@pytest.mark.asyncio
async def test_bootstrap_fresh_login_atomically_replaces_only_after_identity_check(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    client = AuthClient(
        Actor(id=42, username="verified"),
        fresh_token="new-token",
    )

    actor = await bootstrap_account(
        config,
        CoordinationStore(config),
        token_store=store,
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    assert actor.id == 42
    assert client.get_token_calls == 1
    assert client.get_me_calls == 1
    assert client.closed == 1
    saved = load_record(store)
    assert saved is not None
    assert saved.user_id == 42
    assert saved.username == "verified"
    assert saved.token == "new-token"
    assert stat.S_IMODE((config.state_dir / "tokens" / "account-42.json").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_bootstrap_requires_bound_scope_and_exactly_one_source(
    tmp_path: Path,
) -> None:
    unbound = KworkConfig(
        login="prompted-login",
        password="prompted-password",
        state_dir=tmp_path / "unbound",
    )
    with pytest.raises(GatewayError) as binding:
        await bootstrap_account(unbound, CoordinationStore(unbound))
    assert binding.value.code is ErrorCode.ACCOUNT_BINDING_REQUIRED

    both = credentialless_config(
        tmp_path / "both",
        login="prompted-login",
        password="prompted-password",
        token="explicit-token",
    )
    with pytest.raises(GatewayError) as sources:
        await bootstrap_account(both, CoordinationStore(both))
    assert sources.value.code is ErrorCode.AUTH_REQUIRED

    neither = credentialless_config(tmp_path / "neither")
    with pytest.raises(GatewayError) as missing:
        await bootstrap_account(neither, CoordinationStore(neither))
    assert missing.value.code is ErrorCode.AUTH_REQUIRED


@pytest.mark.asyncio
async def test_bootstrap_rejects_expected_username_and_empty_login_token(
    tmp_path: Path,
) -> None:
    mismatch_config = credentialless_config(
        tmp_path / "mismatch",
        login="prompted-login",
        password="prompted-password",
        expected_username="expected",
    )
    mismatch_client = AuthClient(Actor(id=42, username="different"))
    with pytest.raises(GatewayError) as mismatch:
        await bootstrap_account(
            mismatch_config,
            CoordinationStore(mismatch_config),
            client_factory=lambda _: mismatch_client,  # type: ignore[arg-type]
        )
    assert mismatch.value.code is ErrorCode.ACCOUNT_MISMATCH

    empty_config = credentialless_config(
        tmp_path / "empty",
        login="prompted-login",
        password="prompted-password",
    )
    empty_client = AuthClient(
        Actor(id=42, username="fixture"),
        fresh_token="",
    )
    with pytest.raises(GatewayError) as empty:
        await bootstrap_account(
            empty_config,
            CoordinationStore(empty_config),
            client_factory=lambda _: empty_client,  # type: ignore[arg-type]
        )
    assert empty.value.code is ErrorCode.CONTRACT_DRIFT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "expected_code"),
    [
        (Actor(id=99, username="wrong"), ErrorCode.ACCOUNT_MISMATCH),
        (TimeoutError(), ErrorCode.TIMEOUT),
    ],
)
async def test_failed_bootstrap_preserves_existing_record(
    tmp_path: Path,
    actor: Actor | BaseException,
    expected_code: ErrorCode,
) -> None:
    config = credentialless_config(
        tmp_path / f"state-{expected_code}",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    token_path = config.state_dir / "tokens" / "account-42.json"
    before = token_path.read_bytes()
    client = AuthClient(actor, fresh_token="new-token")

    with pytest.raises(GatewayError) as caught:
        await bootstrap_account(
            config,
            CoordinationStore(config),
            token_store=store,
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    assert caught.value.code is expected_code
    assert load_record(store) == old
    assert token_path.read_bytes() == before
    assert client.closed == 1


def test_mismatched_record_save_never_changes_existing_file(tmp_path: Path) -> None:
    store = SecureTokenStore(tmp_path / "state")
    valid = TokenRecord.create(user_id=42, username="valid", token="valid-token")
    save_record(store, valid)
    path = tmp_path / "state" / "tokens" / "account-42.json"
    before = path.read_bytes()
    fd = store.acquire_lock("account-42")
    try:
        with pytest.raises(GatewayError) as caught:
            store.save_locked(
                "account-42",
                TokenRecord.create(
                    user_id=99,
                    username="wrong",
                    token="wrong-token",
                ),
            )
    finally:
        store.release_lock(fd)
    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert path.read_bytes() == before


def test_token_record_repr_never_exposes_token_or_proxy_credentials() -> None:
    record = TokenRecord.create(
        user_id=42,
        username="fixture",
        token="repr-token-sentinel",
        proxy_url="socks5://repr-user:repr-pass@proxy.example:1080",
    )
    rendered = repr(record)
    for secret in (
        "repr-token-sentinel",
        "repr-user",
        "repr-pass",
        "proxy.example",
    ):
        assert secret not in rendered


def test_oversized_or_unsafe_record_fails_before_replacing_store(
    tmp_path: Path,
) -> None:
    store = SecureTokenStore(tmp_path / "state")
    original = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, original)
    path = tmp_path / "state" / "tokens" / "account-42.json"
    before = path.read_bytes()

    for kwargs in (
        {"user_id": 42, "username": "valid", "token": "x" * 16_385},
        {"user_id": 42, "username": "\ud800", "token": "valid"},
        {
            "user_id": 42,
            "username": "valid",
            "token": "valid",
            "proxy_url": "https://proxy.example/path",
        },
    ):
        with pytest.raises(GatewayError) as caught:
            TokenRecord.create(**kwargs)  # type: ignore[arg-type]
        assert caught.value.code is ErrorCode.VALIDATION
        assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_cancelled_token_lock_waiter_does_not_orphan_flock(tmp_path: Path) -> None:
    config = credentialless_config(tmp_path / "state")
    holder = SecureTokenStore(config.state_dir, lock_timeout=1.0)
    held_fd = holder.acquire_lock("account-42")
    waiting_store = SecureTokenStore(config.state_dir, lock_timeout=1.0)
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: AuthClient(Actor(id=42, username="fixture")),  # type: ignore[arg-type]
        token_store=waiting_store,
    )
    task = asyncio.create_task(session.ensure_client())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    holder.release_lock(held_fd)
    with pytest.raises(asyncio.CancelledError):
        await task

    contender = SecureTokenStore(config.state_dir, lock_timeout=0.2)
    fd = contender.acquire_lock("account-42")
    contender.release_lock(fd)


@pytest.mark.asyncio
async def test_credentialless_startup_restores_proxy_from_private_store(
    tmp_path: Path,
) -> None:
    proxy = "socks5://proxy-user:proxy-password@proxy.example:1080"
    config = credentialless_config(tmp_path / "state")
    store = SecureTokenStore(config.state_dir)
    record = TokenRecord.create(
        user_id=42,
        username="fixture",
        token="cached-token",
        proxy_url=proxy,
    )
    save_record(store, record)
    client = AuthClient(Actor(id=42, username="fixture"))
    received: list[KworkConfig] = []

    def factory(active_config: KworkConfig) -> AuthClient:
        received.append(active_config)
        return client

    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=factory,  # type: ignore[arg-type]
        token_store=store,
    )
    await session.ensure_client()

    assert len(received) == 1
    assert received[0].proxy_value == proxy
    assert received[0].login == ""
    assert received[0].password_value == ""
    assert load_record(store) == record


def test_server_secret_environment_detection_is_case_insensitive() -> None:
    assert secret_server_environment_present({"KWORK_PASSWORD": "secret"}) is True
    assert secret_server_environment_present({"kwork_proxy_url": "https://proxy"}) is True
    assert secret_server_environment_present({"KWORK_TOKEN": "   "}) is False
    assert (
        secret_server_environment_present(
            {
                "KWORK_EXPECTED_USER_ID": "42",
                "KWORK_ENABLE_WRITES": "false",
            }
        )
        is False
    )


def _clear_kwork_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.upper().startswith("KWORK_"):
            monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_bootstrap_cli_uses_tty_only_and_ignores_inherited_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_kwork_environment(monkeypatch)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))
    monkeypatch.setenv("KWORK_TOKEN", "inherited-token-sentinel")
    monkeypatch.setenv("KWORK_LOGIN", "inherited-login-sentinel")
    monkeypatch.setenv("KWORK_PASSWORD", "inherited-password-sentinel")
    monkeypatch.setenv("KWORK_PROXY_URL", "https://inherited-proxy-sentinel.example")
    monkeypatch.setenv("KWORK_ENABLE_WRITES", "true")

    stdin = TTYBuffer()
    stdout = io.StringIO()
    stderr = TTYBuffer()
    answers = iter(("prompted-login", "prompted-password", "", ""))
    prompted: list[str] = []

    def getpass_fn(prompt: str, *, stream: Any) -> str:
        assert stream is stderr
        prompted.append(prompt)
        return next(answers)

    client = AuthClient(
        Actor(id=42, username="verified-user"),
        fresh_token="bootstrap-token-sentinel",
    )
    received: list[KworkConfig] = []

    def factory(config: KworkConfig) -> AuthClient:
        received.append(config)
        return client

    code = await run_bootstrap_cli(
        [],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        getpass_fn=getpass_fn,
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=tmp_path / "home",
    )

    assert code == 0
    assert len(prompted) == 4
    assert len(received) == 1
    assert received[0].login == "prompted-login"
    assert received[0].password_value == "prompted-password"
    assert received[0].token_value == ""
    assert received[0].proxy_value is None
    assert received[0].enable_writes is False
    payload = json.loads(stdout.getvalue())
    assert payload == {
        "account": {"user_id": 42, "username": "verified-user"},
        "credential_store": "account_scoped_token",
        "environment": {
            "KWORK_ENABLE_WRITES": "false",
            "KWORK_EXPECTED_USER_ID": "42",
            "KWORK_PERSIST_TOKEN": "true",
        },
        "schema_version": "1.0",
        "verified": True,
    }
    combined_output = stdout.getvalue() + stderr.getvalue()
    for secret in (
        "inherited-token-sentinel",
        "inherited-login-sentinel",
        "inherited-password-sentinel",
        "inherited-proxy-sentinel",
        "prompted-login",
        "prompted-password",
        "bootstrap-token-sentinel",
    ):
        assert secret not in combined_output
    stored = load_record(SecureTokenStore(state_dir))
    assert stored is not None
    assert stored.token == "bootstrap-token-sentinel"
    assert stored.proxy_url is None


@pytest.mark.asyncio
async def test_bootstrap_cli_validates_and_imports_legacy_token_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_kwork_environment(monkeypatch)
    state_dir = tmp_path / "state"
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    legacy_path = home_dir / ".kwork_token"
    legacy_path.write_text("legacy-token-sentinel\n")
    os.chmod(legacy_path, 0o600)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))
    monkeypatch.setenv("HOME", str(home_dir))

    stdin = TTYBuffer("yes\n")
    stdout = io.StringIO()
    stderr = TTYBuffer()
    answers = iter(("socks5://proxy-user:proxy-pass@proxy.example:1080",))

    def getpass_fn(prompt: str, *, stream: Any) -> str:
        assert stream is stderr
        return next(answers)

    client = AuthClient(Actor(id=42, username="legacy-user"))
    received: list[KworkConfig] = []

    def factory(config: KworkConfig) -> AuthClient:
        received.append(config)
        return client

    code = await run_bootstrap_cli(
        [],
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        getpass_fn=getpass_fn,
        client_factory=factory,  # type: ignore[arg-type]
    )

    assert code == 0
    assert client.get_token_calls == 0
    assert client.get_me_calls == 1
    assert received[0].token_value == "legacy-token-sentinel"
    assert received[0].proxy_value == "socks5://proxy-user:proxy-pass@proxy.example:1080"
    payload = json.loads(stdout.getvalue())
    assert payload["legacy_file_retained"] is True
    assert legacy_path.read_text() == "legacy-token-sentinel\n"
    combined_output = stdout.getvalue() + stderr.getvalue()
    assert "legacy-token-sentinel" not in combined_output
    assert "proxy-pass" not in combined_output
    stored = load_record(SecureTokenStore(state_dir))
    assert stored is not None
    assert stored.token == "legacy-token-sentinel"
    assert stored.proxy_url == "socks5://proxy-user:proxy-pass@proxy.example:1080"


@pytest.mark.asyncio
async def test_insecure_legacy_import_fails_before_network_and_preserves_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_kwork_environment(monkeypatch)
    state_dir = tmp_path / "state"
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    legacy_path = home_dir / ".kwork_token"
    legacy_path.write_text("unsafe-legacy-token-sentinel\n")
    os.chmod(legacy_path, 0o644)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))
    store = SecureTokenStore(state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    record_path = state_dir / "tokens" / "account-42.json"
    before = record_path.read_bytes()
    factory_calls = 0

    def factory(_: KworkConfig) -> AuthClient:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("insecure legacy file must fail before network")

    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer("yes\n"),
        stdout=(stdout := io.StringIO()),
        stderr=(stderr := TTYBuffer()),
        getpass_fn=lambda *args, **kwargs: pytest.fail("proxy prompt must not run"),
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=home_dir,
    )

    assert code == 1
    assert stdout.getvalue() == ""
    assert "unsafe-legacy-token-sentinel" not in stderr.getvalue()
    assert factory_calls == 0
    assert record_path.read_bytes() == before


@pytest.mark.asyncio
async def test_bootstrap_cli_rejects_non_tty_and_unknown_argv_without_reflection(
    tmp_path: Path,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = await run_bootstrap_cli(
        ["--token=argv-secret-sentinel"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        home_dir=tmp_path,
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert "argv-secret-sentinel" not in stderr.getvalue()

    stderr = io.StringIO()
    code = await run_bootstrap_cli(
        [],
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=stderr,
        home_dir=tmp_path,
    )
    assert code == 2
    assert "TTY" in stderr.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "expected_fragment"),
    [
        (["--help"], "kwork-mcp-bootstrap"),
        (["-h"], "kwork-mcp-bootstrap"),
        (["--version"], "1.0.0rc1"),
    ],
)
async def test_bootstrap_help_and_version_need_no_tty_or_configuration(
    tmp_path: Path,
    argv: list[str],
    expected_fragment: str,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = await run_bootstrap_cli(
        argv,
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        client_factory=lambda _: pytest.fail("help must not construct a client"),  # type: ignore[arg-type,return-value]
        home_dir=tmp_path,
    )
    assert code == 0
    assert expected_fragment in stdout.getvalue()
    assert stderr.getvalue() == ""
    assert not (tmp_path / ".local").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stdin", "stderr"),
    [
        (io.StringIO(), TTYBuffer()),
        (TTYBuffer(), io.StringIO()),
    ],
)
async def test_bootstrap_requires_both_verified_tty_streams(
    tmp_path: Path,
    stdin: io.StringIO,
    stderr: io.StringIO,
) -> None:
    calls = 0

    def factory(_: KworkConfig) -> AuthClient:
        nonlocal calls
        calls += 1
        raise AssertionError

    code = await run_bootstrap_cli(
        [],
        stdin=stdin,
        stdout=(stdout := io.StringIO()),
        stderr=stderr,
        getpass_fn=lambda *args, **kwargs: pytest.fail("prompt must not run"),
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=tmp_path,
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_index", "failure"),
    [
        (0, bootstrap_module.getpass.GetPassWarning("no tty")),
        (1, EOFError()),
        (2, KeyboardInterrupt()),
        (3, EOFError()),
    ],
)
async def test_bootstrap_prompt_abort_preserves_existing_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_index: int,
    failure: BaseException,
) -> None:
    _clear_kwork_environment(monkeypatch)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))
    store = SecureTokenStore(state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    path = state_dir / "tokens" / "account-42.json"
    before = path.read_bytes()
    answer = "prompt-secret-sentinel"
    calls = 0

    def getpass_fn(prompt: str, *, stream: Any) -> str:
        nonlocal calls
        current = calls
        calls += 1
        if current == failure_index:
            raise failure
        return answer

    client_calls = 0

    def factory(_: KworkConfig) -> AuthClient:
        nonlocal client_calls
        client_calls += 1
        return AuthClient(Actor(id=42, username="unexpected"))

    stdout = io.StringIO()
    stderr = TTYBuffer()
    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer(),
        stdout=stdout,
        stderr=stderr,
        getpass_fn=getpass_fn,
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=tmp_path / "home",
    )
    assert code == 130
    assert stdout.getvalue() == ""
    assert answer not in stderr.getvalue()
    assert client_calls == 0
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        ("hidden-login-sentinel", "hidden-password-sentinel", "123", ""),
        (
            "hidden-login-sentinel",
            "hidden-password-sentinel",
            "",
            "https://proxy.example/path",
        ),
    ],
)
async def test_invalid_hidden_bootstrap_input_is_generic_and_preserves_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: tuple[str, str, str, str],
) -> None:
    _clear_kwork_environment(monkeypatch)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))
    store = SecureTokenStore(state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    path = state_dir / "tokens" / "account-42.json"
    before = path.read_bytes()
    queued = iter(answers)
    client_calls = 0

    def factory(_: KworkConfig) -> AuthClient:
        nonlocal client_calls
        client_calls += 1
        raise AssertionError

    stdout = io.StringIO()
    stderr = TTYBuffer()
    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer(),
        stdout=stdout,
        stderr=stderr,
        getpass_fn=lambda prompt, *, stream: next(queued),
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=tmp_path / "home",
    )
    assert code == 2
    assert stdout.getvalue() == ""
    assert all(value not in stderr.getvalue() for value in answers if value)
    assert client_calls == 0
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_concurrent_bootstrap_is_one_writer_and_never_interleaves_store(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    record_path = config.state_dir / "tokens" / "account-42.json"
    before = record_path.read_bytes()
    entered = asyncio.Event()
    release = asyncio.Event()
    first_client = BlockingAuthClient(
        Actor(id=42, username="new"),
        entered=entered,
        release=release,
        fresh_token="new-token",
    )
    second_factory_calls = 0

    async def first_bootstrap() -> Actor:
        return await bootstrap_account(
            config,
            CoordinationStore(config),
            token_store=store,
            client_factory=lambda _: first_client,  # type: ignore[arg-type]
        )

    first = asyncio.create_task(first_bootstrap())
    await entered.wait()

    def second_factory(_: KworkConfig) -> AuthClient:
        nonlocal second_factory_calls
        second_factory_calls += 1
        return AuthClient(Actor(id=42, username="second"))

    with pytest.raises(GatewayError) as caught:
        await bootstrap_account(
            config,
            CoordinationStore(config),
            token_store=store,
            client_factory=second_factory,  # type: ignore[arg-type]
        )
    assert caught.value.code is ErrorCode.WRITE_IN_PROGRESS
    assert second_factory_calls == 0
    assert record_path.read_bytes() == before

    release.set()
    assert (await first).id == 42
    saved = load_record(store)
    assert saved is not None
    assert saved.token == "new-token"


@pytest.mark.asyncio
async def test_bootstrap_authentication_uses_shared_route_coordination(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    coordinator = RecordingBootstrapCoordinator(config)
    client = AuthClient(Actor(id=42, username="verified"))
    await bootstrap_account(
        config,
        coordinator,
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    assert coordinator.acquired == [
        ("account-42", "signIn"),
        ("account-42", "actor"),
    ]
    assert coordinator.successes == coordinator.acquired
    assert coordinator.failures == []


@pytest.mark.asyncio
async def test_cancelled_bootstrap_before_commit_preserves_store_and_releases_locks(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    path = config.state_dir / "tokens" / "account-42.json"
    before = path.read_bytes()
    entered = asyncio.Event()
    release = asyncio.Event()
    client = BlockingAuthClient(
        Actor(id=42, username="new"),
        entered=entered,
        release=release,
        fresh_token="new-token",
    )
    coordinator = CoordinationStore(config)
    task = asyncio.create_task(
        bootstrap_account(
            config,
            coordinator,
            token_store=store,
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert path.read_bytes() == before
    assert client.closed == 1
    fd = store.acquire_lock("account-42")
    store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_cancellation_during_close_after_commit_is_typed_unknown(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    client = BlockingCloseAuthClient(
        Actor(id=42, username="new"),
        close_entered=close_entered,
        close_release=close_release,
        fresh_token="new-token",
    )
    task = asyncio.create_task(
        bootstrap_account(
            config,
            CoordinationStore(config),
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    )
    await close_entered.wait()
    task.cancel()
    close_release.set()
    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(task, timeout=0.5)

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    stored = load_record(SecureTokenStore(config.state_dir))
    assert stored is not None
    assert stored.token == "new-token"
    assert client.closed == 1
    store = SecureTokenStore(config.state_dir)
    fd = store.acquire_lock("account-42")
    store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_bootstrap_close_helper_reports_child_cancellation_without_spin() -> None:
    client = SelfCancellingCloseAuthClient(Actor(id=42, username="verified"))

    outcome = await asyncio.wait_for(
        bootstrap_module._close_bootstrap_client(client),
        timeout=0.5,
    )

    assert outcome.child_cancelled is True
    assert outcome.caller_cancelled is False
    assert client.closed == 1


@pytest.mark.asyncio
async def test_bootstrap_close_helper_bounds_terminal_cleanup_branches() -> None:
    class FailingClose:
        async def close(self) -> None:
            raise RuntimeError("synthetic close failure")

    class CancelReturns:
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

    class CancelRaises:
        async def close(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                raise RuntimeError("synthetic cancel cleanup failure") from exc

    failed = await asyncio.wait_for(
        bootstrap_module._close_bootstrap_client(FailingClose()),
        timeout=0.5,
    )
    returned = await asyncio.wait_for(
        bootstrap_module._close_bootstrap_client(
            CancelReturns(),
            timeout_seconds=0.01,
            cancel_grace_seconds=0.1,
        ),
        timeout=0.5,
    )
    raised = await asyncio.wait_for(
        bootstrap_module._close_bootstrap_client(
            CancelRaises(),
            timeout_seconds=0.01,
            cancel_grace_seconds=0.1,
        ),
        timeout=0.5,
    )
    detached = await asyncio.wait_for(
        bootstrap_module._close_bootstrap_client(
            CancelReturns(),
            timeout_seconds=0.0,
            cancel_grace_seconds=0.0,
        ),
        timeout=0.5,
    )
    await asyncio.sleep(0)

    assert failed.cancelled is False
    assert returned.timed_out is True
    assert raised.timed_out is True
    assert detached.timed_out is True


@pytest.mark.asyncio
async def test_bootstrap_child_close_cancel_before_commit_preserves_primary_error_and_store(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    path = config.state_dir / "tokens" / "account-42.json"
    before = path.read_bytes()
    client = SelfCancellingCloseAuthClient(
        Actor(id=99, username="wrong"),
        fresh_token="unused-new-token",
    )
    coordinator = CoordinationStore(config)

    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(
            bootstrap_account(
                config,
                coordinator,
                token_store=store,
                client_factory=lambda _: client,  # type: ignore[arg-type]
            ),
            timeout=0.5,
        )

    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert path.read_bytes() == before
    assert client.closed == 1
    fd = store.acquire_lock("account-42")
    store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_bootstrap_child_close_cancel_after_commit_is_typed_unknown(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    client = SelfCancellingCloseAuthClient(
        Actor(id=42, username="new"),
        fresh_token="new-token",
    )
    coordinator = CoordinationStore(config)

    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(
            bootstrap_account(
                config,
                coordinator,
                token_store=store,
                client_factory=lambda _: client,  # type: ignore[arg-type]
            ),
            timeout=0.5,
        )

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    stored = load_record(store)
    assert stored is not None
    assert stored.token == "new-token"
    assert client.closed == 1
    fd = store.acquire_lock("account-42")
    store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_bootstrap_typed_unknown_survives_cancellation_during_lock_release(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    release_started = threading.Event()
    release_allowed = threading.Event()
    store = BlockingReleaseTokenStore(
        config.state_dir,
        release_started=release_started,
        release_allowed=release_allowed,
    )
    client = SelfCancellingCloseAuthClient(
        Actor(id=42, username="new"),
        fresh_token="new-token",
    )
    task = asyncio.create_task(
        bootstrap_account(
            config,
            CoordinationStore(config),
            token_store=store,
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    )
    assert await asyncio.to_thread(release_started.wait, 2)
    task.cancel()
    release_allowed.set()

    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(task, timeout=0.5)

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    normal_store = SecureTokenStore(config.state_dir)
    stored = load_record(normal_store)
    assert stored is not None
    assert stored.token == "new-token"
    assert client.closed == 1
    fd = normal_store.acquire_lock("account-42")
    normal_store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_bootstrap_first_cancellation_during_lock_release_after_commit_is_unknown(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    release_started = threading.Event()
    release_allowed = threading.Event()
    store = BlockingReleaseTokenStore(
        config.state_dir,
        release_started=release_started,
        release_allowed=release_allowed,
    )
    client = AuthClient(
        Actor(id=42, username="new"),
        fresh_token="new-token",
    )
    task = asyncio.create_task(
        bootstrap_account(
            config,
            CoordinationStore(config),
            token_store=store,
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    )
    assert await asyncio.to_thread(release_started.wait, 2)
    task.cancel()
    release_allowed.set()

    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(task, timeout=0.5)

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    normal_store = SecureTokenStore(config.state_dir)
    stored = load_record(normal_store)
    assert stored is not None
    assert stored.token == "new-token"
    assert client.closed == 1
    fd = normal_store.acquire_lock("account-42")
    normal_store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_post_replace_unknown_survives_caller_cancel_during_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_post_replace_directory_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("synthetic post-replace durability failure")
        real_fsync(fd)

    monkeypatch.setattr(security_module.os, "fsync", fail_post_replace_directory_fsync)
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    client = BlockingCloseAuthClient(
        Actor(id=42, username="new"),
        close_entered=close_entered,
        close_release=close_release,
        fresh_token="new-token",
    )
    coordinator = CoordinationStore(config)
    task = asyncio.create_task(
        bootstrap_account(
            config,
            coordinator,
            token_store=store,
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(close_entered.wait(), timeout=0.5)
    task.cancel()
    close_release.set()

    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(task, timeout=0.5)

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    stored = load_record(store)
    assert stored is not None
    assert stored.token == "new-token"
    assert client.closed == 1
    fd = store.acquire_lock("account-42")
    store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_post_replace_primary_survives_release_error_and_unlocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from loguru import logger

    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    normal_store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(normal_store, old)
    protected_detail = "synthetic-release-protected-detail"
    store = RaisingAfterReleaseTokenStore(
        config.state_dir,
        protected_detail=protected_detail,
    )
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_post_replace_directory_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("synthetic post-replace durability failure")
        real_fsync(fd)

    monkeypatch.setattr(security_module.os, "fsync", fail_post_replace_directory_fsync)
    client = AuthClient(
        Actor(id=42, username="new"),
        fresh_token="new-token",
    )
    log_messages: list[str] = []
    sink_id = logger.add(
        lambda message: log_messages.append(str(message)),
        format="{message}",
    )
    try:
        with pytest.raises(GatewayError) as caught:
            await asyncio.wait_for(
                bootstrap_account(
                    config,
                    CoordinationStore(config),
                    token_store=store,
                    client_factory=lambda _: client,  # type: ignore[arg-type]
                ),
                timeout=0.5,
            )
    finally:
        logger.remove(sink_id)

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    assert protected_detail not in str(caught.value)
    assert protected_detail not in "".join(log_messages)
    stored = load_record(normal_store)
    assert stored is not None
    assert stored.token == "new-token"
    assert client.closed == 1
    fd = normal_store.acquire_lock("account-42")
    normal_store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_ordinary_primary_survives_release_error_and_preserves_store(
    tmp_path: Path,
) -> None:
    from loguru import logger

    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    normal_store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(normal_store, old)
    token_path = config.state_dir / "tokens" / "account-42.json"
    before = token_path.read_bytes()
    protected_detail = "synthetic-ordinary-release-protected-detail"
    store = RaisingAfterReleaseTokenStore(
        config.state_dir,
        protected_detail=protected_detail,
    )
    client = AuthClient(
        Actor(id=99, username="wrong"),
        fresh_token="unused-new-token",
    )
    log_messages: list[str] = []
    sink_id = logger.add(
        lambda message: log_messages.append(str(message)),
        format="{message}",
    )
    try:
        with pytest.raises(GatewayError) as caught:
            await asyncio.wait_for(
                bootstrap_account(
                    config,
                    CoordinationStore(config),
                    token_store=store,
                    client_factory=lambda _: client,  # type: ignore[arg-type]
                ),
                timeout=0.5,
            )
    finally:
        logger.remove(sink_id)

    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert protected_detail not in str(caught.value)
    assert protected_detail not in "".join(log_messages)
    assert token_path.read_bytes() == before
    assert load_record(normal_store) == old
    assert client.closed == 1
    fd = normal_store.acquire_lock("account-42")
    normal_store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_release_error_without_primary_after_commit_is_typed_unknown(
    tmp_path: Path,
) -> None:
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    protected_detail = "synthetic-lone-release-protected-detail"
    store = RaisingAfterReleaseTokenStore(
        config.state_dir,
        protected_detail=protected_detail,
    )
    client = AuthClient(
        Actor(id=42, username="new"),
        fresh_token="new-token",
    )

    with pytest.raises(GatewayError) as caught:
        await asyncio.wait_for(
            bootstrap_account(
                config,
                CoordinationStore(config),
                token_store=store,
                client_factory=lambda _: client,  # type: ignore[arg-type]
            ),
            timeout=0.5,
        )

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    assert caught.value.diagnostic == "bootstrap_lock_release_failed_after_store_commit"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert protected_detail not in str(caught.value)
    normal_store = SecureTokenStore(config.state_dir)
    stored = load_record(normal_store)
    assert stored is not None
    assert stored.token == "new-token"
    assert client.closed == 1
    fd = normal_store.acquire_lock("account-42")
    normal_store.release_lock(fd)
    async with CoordinationStore(config).writer_guard("account-42"):
        pass


@pytest.mark.asyncio
async def test_hanging_close_before_commit_is_bounded_and_preserves_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_module, "_BOOTSTRAP_CLOSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(bootstrap_module, "_BOOTSTRAP_CLOSE_CANCEL_GRACE_SECONDS", 0.02)
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    path = config.state_dir / "tokens" / "account-42.json"
    before = path.read_bytes()
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    close_finished = asyncio.Event()
    client = CancellationResistantCloseAuthClient(
        Actor(id=99, username="wrong"),
        close_entered=close_entered,
        close_release=close_release,
        close_finished=close_finished,
    )
    coordinator = CoordinationStore(config)
    task = asyncio.create_task(
        bootstrap_account(
            config,
            coordinator,
            token_store=store,
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(close_entered.wait(), timeout=0.5)
    try:
        with pytest.raises(GatewayError) as caught:
            await asyncio.wait_for(task, timeout=0.5)

        assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
        assert path.read_bytes() == before
        fd = store.acquire_lock("account-42")
        store.release_lock(fd)
        async with CoordinationStore(config).writer_guard("account-42"):
            pass
    finally:
        close_release.set()
        await asyncio.wait_for(close_finished.wait(), timeout=0.5)
    assert client.closed == 1


@pytest.mark.asyncio
async def test_hanging_close_after_commit_is_bounded_typed_unknown_and_unlocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap_module, "_BOOTSTRAP_CLOSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(bootstrap_module, "_BOOTSTRAP_CLOSE_CANCEL_GRACE_SECONDS", 0.02)
    config = credentialless_config(
        tmp_path / "state",
        login="prompted-login",
        password="prompted-password",
    )
    store = SecureTokenStore(config.state_dir)
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    close_finished = asyncio.Event()
    client = CancellationResistantCloseAuthClient(
        Actor(id=42, username="new"),
        close_entered=close_entered,
        close_release=close_release,
        close_finished=close_finished,
        fresh_token="new-token",
    )
    coordinator = CoordinationStore(config)
    task = asyncio.create_task(
        bootstrap_account(
            config,
            coordinator,
            token_store=store,
            client_factory=lambda _: client,  # type: ignore[arg-type]
        )
    )
    await asyncio.wait_for(close_entered.wait(), timeout=0.5)
    task.cancel()
    try:
        with pytest.raises(GatewayError) as caught:
            await asyncio.wait_for(task, timeout=0.5)

        assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
        assert caught.value.reconciliation_required is True
        stored = load_record(store)
        assert stored is not None
        assert stored.token == "new-token"
        fd = store.acquire_lock("account-42")
        store.release_lock(fd)
        async with CoordinationStore(config).writer_guard("account-42"):
            pass
    finally:
        close_release.set()
        await asyncio.wait_for(close_finished.wait(), timeout=0.5)
    assert client.closed == 1


def test_token_replace_post_commit_failure_is_typed_as_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SecureTokenStore(tmp_path / "state")
    old = TokenRecord.create(user_id=42, username="old", token="old-token")
    save_record(store, old)
    replacement = TokenRecord.create(
        user_id=42,
        username="new",
        token="new-token",
    )
    real_fsync = os.fsync
    fsync_calls = 0

    def fail_directory_fsync(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError("synthetic post-replace failure")
        real_fsync(fd)

    monkeypatch.setattr(security_module.os, "fsync", fail_directory_fsync)
    fd = store.acquire_lock("account-42")
    try:
        with pytest.raises(GatewayError) as caught:
            store.save_locked("account-42", replacement)
    finally:
        store.release_lock(fd)

    assert caught.value.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
    assert caught.value.reconciliation_required is True
    assert load_record(store) == replacement


def test_read_private_secret_file_accepts_one_terminal_newline(tmp_path: Path) -> None:
    path = tmp_path / "legacy-token"
    path.write_text("valid-token\n")
    os.chmod(path, 0o600)
    assert read_private_secret_file(path) == "valid-token"
    path.write_bytes(b"valid-token\r\n")
    assert read_private_secret_file(path) == "valid-token"


@pytest.mark.parametrize(
    "kind",
    [
        "symlink",
        "directory",
        "wrong_mode",
        "oversize",
        "invalid_utf8",
        "multiline",
        "multiple_newlines",
        "leading_tab",
        "bidi_control",
    ],
)
def test_read_private_secret_file_rejects_unsafe_sources(
    tmp_path: Path,
    kind: str,
) -> None:
    path = tmp_path / "legacy-token"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("token")
        os.chmod(target, 0o600)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "wrong_mode":
        path.write_text("token")
        os.chmod(path, 0o640)
    elif kind == "oversize":
        path.write_text("x" * 4097)
        os.chmod(path, 0o600)
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff\xfe")
        os.chmod(path, 0o600)
    elif kind == "multiple_newlines":
        path.write_text("token\n\n")
        os.chmod(path, 0o600)
    elif kind == "leading_tab":
        path.write_text("\ttoken\n")
        os.chmod(path, 0o600)
    elif kind == "bidi_control":
        path.write_text("token\u202e")
        os.chmod(path, 0o600)
    else:
        path.write_text("first\nsecond\n")
        os.chmod(path, 0o600)

    with pytest.raises(GatewayError) as caught:
        read_private_secret_file(path)
    assert caught.value.code is ErrorCode.VALIDATION


def test_read_private_secret_file_rejects_wrong_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "legacy-token"
    path.write_text("token")
    os.chmod(path, 0o600)
    actual_uid = os.geteuid()
    monkeypatch.setattr(security_module.os, "geteuid", lambda: actual_uid + 1)
    with pytest.raises(GatewayError, match="Параметры"):
        read_private_secret_file(path)


def test_read_private_secret_file_detects_lstat_open_inode_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "legacy-token"
    path.write_text("first-token")
    os.chmod(path, 0o600)
    replacement = tmp_path / "replacement"
    replacement.write_text("second-token")
    os.chmod(replacement, 0o600)
    real_open = os.open
    swapped = False

    def swapping_open(target: Any, flags: int, mode: int = 0o777) -> int:
        nonlocal swapped
        if not swapped and Path(target) == path:
            swapped = True
            path.unlink()
            replacement.rename(path)
        return real_open(target, flags, mode)

    monkeypatch.setattr(security_module.os, "open", swapping_open)
    with pytest.raises(GatewayError, match="Параметры"):
        read_private_secret_file(path)
    assert swapped is True


@pytest.mark.asyncio
async def test_declining_legacy_import_never_reads_or_removes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_kwork_environment(monkeypatch)
    state_dir = tmp_path / "state"
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    legacy_path = home_dir / ".kwork_token"
    legacy_path.write_text("declined-token-sentinel")
    os.chmod(legacy_path, 0o600)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "42")
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))
    read_calls = 0

    def forbidden_read(*args: Any, **kwargs: Any) -> str:
        nonlocal read_calls
        read_calls += 1
        raise AssertionError("declined legacy source must not be opened")

    monkeypatch.setattr(bootstrap_module, "read_private_secret_file", forbidden_read)
    answers = iter(("fresh-login", "fresh-password", "", ""))
    client = AuthClient(Actor(id=42, username="fresh-user"))
    stdout = io.StringIO()
    stderr = TTYBuffer()
    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer("no\n"),
        stdout=stdout,
        stderr=stderr,
        getpass_fn=lambda prompt, *, stream: next(answers),
        client_factory=lambda _: client,  # type: ignore[arg-type]
        home_dir=home_dir,
    )

    assert code == 0
    assert read_calls == 0
    assert legacy_path.read_text() == "declined-token-sentinel"
    assert "declined-token-sentinel" not in stdout.getvalue() + stderr.getvalue()
