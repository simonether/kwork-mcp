from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from kwork.exceptions import KworkHTTPException
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode
from kwork_mcp.security import SecureTokenStore, TokenRecord
from kwork_mcp.session import KworkSessionManager


class FakeClient:
    def __init__(
        self,
        actors: list[Actor | BaseException],
        *,
        fresh_token: str = "fresh-token",
        web_status: int | None = 200,
    ) -> None:
        self._actors = list(actors)
        self._token: str | None = None
        self.fresh_token = fresh_token
        self.web_status = web_status
        self.get_me_calls = 0
        self.get_token_calls = 0
        self.closed = False

    async def get_me(self) -> Actor:
        self.get_me_calls += 1
        outcome = self._actors.pop(0) if len(self._actors) > 1 else self._actors[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_token(self) -> str:
        self.get_token_calls += 1
        self._token = self.fresh_token
        return self.fresh_token

    async def close(self) -> None:
        self.closed = True

    async def web_login(self, *, url_to_redirect: str) -> SimpleNamespace:
        assert url_to_redirect == "/exchange"
        return SimpleNamespace(status=self.web_status)


def unauthorized() -> KworkHTTPException:
    return KworkHTTPException("expired", status=401, response_json={"success": False})


@pytest.mark.asyncio
async def test_correct_account_binds_explicit_token(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(expected_user_id=42)
    coordinator = CoordinationStore(config)
    client = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(config, coordinator, client_factory=lambda _: client)  # type: ignore[arg-type]
    assert await session.ensure_client() is client
    assert session.scope == "account-42"
    assert client._token == "fixture-token"


@pytest.mark.asyncio
async def test_wrong_account_fails_closed(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(expected_user_id=42)
    coordinator = CoordinationStore(config)
    client = FakeClient([Actor(id=99, username="intruder")])
    session = KworkSessionManager(config, coordinator, client_factory=lambda _: client)  # type: ignore[arg-type]
    with pytest.raises(GatewayError) as caught:
        await session.ensure_client()
    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert client.closed is True


@pytest.mark.asyncio
async def test_expired_explicit_token_is_not_reused_during_fresh_login(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(login="user@example.test", password="password")
    coordinator = CoordinationStore(config)
    expired = FakeClient([unauthorized()])
    fresh = FakeClient([Actor(id=42, username="fixture")])
    clients = iter([expired, fresh])
    session = KworkSessionManager(
        config,
        coordinator,
        client_factory=lambda _: next(clients),  # type: ignore[arg-type]
    )
    assert await session.ensure_client() is fresh
    assert expired._token == "fixture-token"
    assert expired.closed is True
    assert fresh.get_token_calls == 1
    assert fresh._token == "fresh-token"


@pytest.mark.asyncio
async def test_read_401_reauthenticates_once_then_returns_data(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        login="user@example.test",
        password="password",
        expected_user_id=42,
        read_attempts=1,
    )
    coordinator = CoordinationStore(config)
    initial = FakeClient([Actor(id=42, username="fixture")])
    replacement = FakeClient([Actor(id=42, username="fixture")])
    clients = iter([initial, replacement])
    session = KworkSessionManager(
        config,
        coordinator,
        client_factory=lambda _: next(clients),  # type: ignore[arg-type]
    )
    calls = 0

    async def operation(client: Any) -> str:
        nonlocal calls
        calls += 1
        if client is initial:
            raise unauthorized()
        return "ok"

    assert await session.call_read("fixture", operation) == "ok"
    assert calls == 2
    assert initial.closed is True
    assert replacement.get_token_calls == 1


@pytest.mark.asyncio
async def test_lagging_process_adopts_peer_refreshed_token_without_second_login(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        login="user@example.test",
        password="password",
        expected_user_id=42,
        persist_token=True,
        read_attempts=1,
    )
    coordinator_a = CoordinationStore(config)
    coordinator_b = CoordinationStore(config)
    initial_a = FakeClient([Actor(id=42, username="fixture")])
    initial_b = FakeClient([Actor(id=42, username="fixture")])
    refreshed_by_a = FakeClient(
        [Actor(id=42, username="fixture")],
        fresh_token="peer-fresh-token",
    )
    adopted_by_b = FakeClient([Actor(id=42, username="fixture")])
    clients_a = iter((initial_a, refreshed_by_a))
    clients_b = iter((initial_b, adopted_by_b))
    session_a = KworkSessionManager(
        config,
        coordinator_a,
        client_factory=lambda _: next(clients_a),  # type: ignore[arg-type]
    )
    session_b = KworkSessionManager(
        config,
        coordinator_b,
        client_factory=lambda _: next(clients_b),  # type: ignore[arg-type]
    )
    assert await session_a.ensure_client() is initial_a
    assert await session_b.ensure_client() is initial_b

    async def read_after_expiry(client: Any, stale: FakeClient) -> str:
        if client is stale:
            raise unauthorized()
        return "ok"

    assert (
        await session_a.call_read(
            "projects",
            lambda client: read_after_expiry(client, initial_a),
        )
        == "ok"
    )
    assert (
        await session_b.call_read(
            "projects",
            lambda client: read_after_expiry(client, initial_b),
        )
        == "ok"
    )

    assert refreshed_by_a.get_token_calls == 1
    assert adopted_by_b.get_token_calls == 0
    assert adopted_by_b._token == "peer-fresh-token"
    store = SecureTokenStore(config.state_dir)
    lock_fd = store.acquire_lock(config.bootstrap_scope)
    try:
        record = store.load_locked(config.bootstrap_scope)
    finally:
        store.release_lock(lock_fd)
    assert record is not None
    assert record.token == "peer-fresh-token"


class CacheSpy:
    def __init__(self) -> None:
        self.loads = 0

    def acquire_lock(self, scope: str) -> int:
        return 1

    def release_lock(self, fd: int) -> None:
        return None

    def load_locked(self, scope: str) -> TokenRecord:
        self.loads += 1
        return TokenRecord.create(user_id=42, username="cached", token="cached-token")

    def save_locked(self, scope: str, record: TokenRecord) -> None:
        return None

    def delete_locked(self, scope: str) -> None:
        return None


@pytest.mark.asyncio
async def test_token_only_unbound_session_never_uses_persisted_cache(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(persist_token=True)
    coordinator = CoordinationStore(config)
    expired = FakeClient([unauthorized()])
    cache = CacheSpy()
    session = KworkSessionManager(
        config,
        coordinator,
        client_factory=lambda _: expired,  # type: ignore[arg-type]
        token_store=cache,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as caught:
        await session.ensure_client()
    assert caught.value.code is ErrorCode.AUTH_EXPIRED
    assert cache.loads == 0
    assert config.token_cache_is_bound is False


@pytest.mark.asyncio
async def test_bound_session_can_use_identity_scoped_cache(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        token=None,
        login="user@example.test",
        password="password",
        persist_token=True,
        expected_user_id=42,
    )
    coordinator = CoordinationStore(config)
    cached_client = FakeClient([Actor(id=42, username="cached")])
    cache = CacheSpy()
    session = KworkSessionManager(
        config,
        coordinator,
        client_factory=lambda _: cached_client,  # type: ignore[arg-type]
        token_store=cache,  # type: ignore[arg-type]
    )
    assert await session.ensure_client() is cached_client
    assert cache.loads == 1
    assert cached_client._token == "cached-token"
    assert cached_client.get_token_calls == 0


@pytest.mark.asyncio
async def test_transient_reads_retry_but_write_steps_never_retry(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(read_attempts=2)
    client = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    read_calls = 0

    async def read_operation(_: Any) -> str:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            raise TimeoutError
        return "read-ok"

    assert await session.call_read("projects", read_operation) == "read-ok"
    assert read_calls == 2

    write_calls = 0

    async def write_operation(_: Any) -> str:
        nonlocal write_calls
        write_calls += 1
        raise TimeoutError

    with pytest.raises(GatewayError) as caught:
        await session.call_write_step("write-delete-offer", write_operation)
    assert caught.value.code is ErrorCode.TIMEOUT
    assert write_calls == 1


@pytest.mark.asyncio
async def test_exclusive_client_is_reentrant_and_web_login_is_cached(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    client = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    async with session.exclusive_client(), session.exclusive_client():
        assert await session.ensure_web_client() is client
    assert await session.ensure_web_client() is client
    await session.close()
    assert client.closed is True
    assert session.actor is None


@pytest.mark.asyncio
async def test_web_login_status_and_unbound_identity_fail_closed(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    rejected = FakeClient(
        [Actor(id=42, username="fixture")],
        web_status=500,
    )
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: rejected,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as web_error:
        await session.ensure_web_client()
    assert web_error.value.code is ErrorCode.AUTH_EXPIRED
    with pytest.raises(GatewayError) as binding_error:
        await session.verify_account_identity()
    assert binding_error.value.code is ErrorCode.ACCOUNT_BINDING_REQUIRED


@pytest.mark.asyncio
async def test_writes_require_enablement_and_fresh_expected_identity(
    config_factory: Callable[..., KworkConfig],
) -> None:
    disabled_config = config_factory(expected_user_id=42)
    disabled = KworkSessionManager(
        disabled_config,
        CoordinationStore(disabled_config),
        client_factory=lambda _: FakeClient([Actor(id=42, username="fixture")]),  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as caught:
        await disabled.verify_write_identity()
    assert caught.value.code is ErrorCode.WRITE_DISABLED

    enabled_config = config_factory(expected_user_id=42, enable_writes=True)
    changed = FakeClient(
        [
            Actor(id=42, username="fixture"),
            Actor(id=99, username="intruder"),
        ]
    )
    enabled = KworkSessionManager(
        enabled_config,
        CoordinationStore(enabled_config),
        client_factory=lambda _: changed,  # type: ignore[arg-type]
    )
    await enabled.ensure_client()
    with pytest.raises(GatewayError) as mismatch:
        await enabled.verify_write_identity()
    assert mismatch.value.code is ErrorCode.ACCOUNT_MISMATCH
