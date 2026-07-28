from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from kwork.exceptions import KworkHTTPException
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode
from kwork_mcp.security import TokenRecord
from kwork_mcp.session import KworkSessionManager, _canonical_route


def unauthorized() -> KworkHTTPException:
    return KworkHTTPException("expired", status=401, response_json={"success": False})


class RecordingCoordinator:
    def __init__(self) -> None:
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


class FakeClient:
    def __init__(
        self,
        actors: list[Actor | BaseException],
        *,
        tokens: list[str | BaseException] | None = None,
        web_result: object | BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.actors = list(actors)
        self.tokens = list(tokens or ["fresh"])
        self.web_result = web_result or SimpleNamespace(status=200)
        self.close_error = close_error
        self._token: str | None = None
        self.closed = 0

    async def get_me(self) -> Actor:
        outcome = self.actors.pop(0) if len(self.actors) > 1 else self.actors[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def get_token(self) -> str:
        outcome = self.tokens.pop(0) if len(self.tokens) > 1 else self.tokens[0]
        if isinstance(outcome, BaseException):
            raise outcome
        self._token = outcome
        return outcome

    async def web_login(self, *, url_to_redirect: str) -> object:
        assert url_to_redirect == "/exchange"
        if isinstance(self.web_result, BaseException):
            raise self.web_result
        return self.web_result

    async def close(self) -> None:
        self.closed += 1
        if self.close_error:
            raise self.close_error


class CacheSpy:
    def __init__(self, record: TokenRecord | None = None) -> None:
        self.record = record
        self.saved: list[TokenRecord] = []
        self.deleted: list[str] = []
        self.releases: list[int] = []

    def acquire_lock(self, _scope: str) -> int:
        return 9

    def release_lock(self, fd: int) -> None:
        self.releases.append(fd)

    def load_locked(self, _scope: str) -> TokenRecord | None:
        return self.record

    def save_locked(self, _scope: str, record: TokenRecord) -> None:
        self.saved.append(record)

    def delete_locked(self, scope: str) -> None:
        self.deleted.append(scope)
        self.record = None


def test_canonical_routes_map_known_and_preserve_unknown() -> None:
    assert _canonical_route("account") == "actor"
    assert _canonical_route("future-route") == "future-route"


@pytest.mark.asyncio
async def test_actor_contract_and_username_binding_fail_closed(
    config_factory: Callable[..., KworkConfig],
) -> None:
    coordinator = RecordingCoordinator()
    for actor, diagnostic in [
        (Actor(id=0, username="fixture"), "actor_missing_stable_identity"),
        (Actor(id=42, username=None), "actor_missing_stable_identity"),
    ]:
        config = config_factory(expected_user_id=42)
        session = KworkSessionManager(
            config,
            coordinator,  # type: ignore[arg-type]
            client_factory=lambda _config, actor=actor: FakeClient([actor]),  # type: ignore[arg-type]
        )
        with pytest.raises(GatewayError) as caught:
            await session.ensure_client()
        assert caught.value.code is ErrorCode.CONTRACT_DRIFT
        assert caught.value.diagnostic == diagnostic

    config = config_factory(expected_user_id=42, expected_username="@Expected")
    client = FakeClient([Actor(id=42, username="different")])
    session = KworkSessionManager(
        config,
        coordinator,  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as mismatch:
        await session.ensure_client()
    assert mismatch.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert mismatch.value.diagnostic == "expected_username_mismatch"


@pytest.mark.asyncio
async def test_validate_token_records_transient_failure_and_closes_client(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    coordinator = RecordingCoordinator()
    client = FakeClient([TimeoutError()])
    session = KworkSessionManager(
        config,
        coordinator,  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as caught:
        await session.ensure_client()
    assert caught.value.code is ErrorCode.TIMEOUT
    assert client.closed == 1
    assert coordinator.failures == [
        (config.bootstrap_scope, "actor", None),
    ]


@pytest.mark.asyncio
async def test_authentication_cancellation_closes_partial_clients_and_releases_lock(
    config_factory: Callable[..., KworkConfig],
) -> None:
    explicit_config = config_factory(expected_user_id=42)
    explicit_cache = CacheSpy()
    explicit = FakeClient([asyncio.CancelledError()])
    explicit_session = KworkSessionManager(
        explicit_config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: explicit,  # type: ignore[arg-type]
        token_store=explicit_cache,  # type: ignore[arg-type]
    )
    with pytest.raises(asyncio.CancelledError):
        await explicit_session.ensure_client()
    assert explicit.closed == 1
    assert explicit_cache.releases == [9]

    login_config = config_factory(
        token=None,
        login="login",
        password="password",
    )
    login_cache = CacheSpy()
    login = FakeClient(
        [Actor(id=42, username="fixture")],
        tokens=[asyncio.CancelledError()],
    )
    login_session = KworkSessionManager(
        login_config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: login,  # type: ignore[arg-type]
        token_store=login_cache,  # type: ignore[arg-type]
    )
    with pytest.raises(asyncio.CancelledError):
        await login_session.ensure_client()
    assert login.closed == 1
    assert login_cache.releases == [9]


@pytest.mark.asyncio
async def test_stored_username_change_refreshes_metadata(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        token=None,
        login="login",
        password="password",
        expected_user_id=42,
        persist_token=True,
    )
    record = TokenRecord.create(user_id=42, username="old", token="cached")
    cache = CacheSpy(record)
    client = FakeClient([Actor(id=42, username="new")])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
        token_store=cache,  # type: ignore[arg-type]
    )
    assert await session.ensure_client() is client
    assert cache.saved[-1].user_id == 42
    assert cache.saved[-1].username == "new"
    assert cache.saved[-1].token == "cached"
    assert client.closed == 0
    assert cache.releases == [9]


@pytest.mark.asyncio
async def test_fresh_login_requires_credentials_and_nonempty_token(
    config_factory: Callable[..., KworkConfig],
) -> None:
    rejected_explicit_only = config_factory()
    session = KworkSessionManager(
        rejected_explicit_only,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: FakeClient([unauthorized()]),  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as missing:
        await session.ensure_client()
    assert missing.value.code is ErrorCode.AUTH_EXPIRED
    assert missing.value.diagnostic == "stored_token_rejected_no_credentials"

    config = config_factory(
        token=None,
        login="login",
        password="password",
    )
    client = FakeClient([Actor(id=42, username="fixture")], tokens=[""])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as empty:
        await session.ensure_client()
    assert empty.value.code is ErrorCode.CONTRACT_DRIFT
    assert empty.value.diagnostic == "empty_login_token"
    assert client.closed == 1


@pytest.mark.asyncio
async def test_fresh_login_transport_failure_records_circuit(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        token=None,
        login="login",
        password="password",
    )
    coordinator = RecordingCoordinator()
    client = FakeClient([Actor(id=42, username="fixture")], tokens=[TimeoutError()])
    session = KworkSessionManager(
        config,
        coordinator,  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as caught:
        await session.ensure_client()
    assert caught.value.code is ErrorCode.TIMEOUT
    assert coordinator.failures == [(config.bootstrap_scope, "signIn", None)]


@pytest.mark.asyncio
async def test_invalid_explicit_token_is_not_retried_from_identical_cache(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        login="login",
        password="password",
        expected_user_id=42,
        persist_token=True,
    )
    cache = CacheSpy(
        TokenRecord.create(
            user_id=42,
            username="fixture",
            token="fixture-token",
        )
    )
    expired = FakeClient([unauthorized()])
    fresh = FakeClient([Actor(id=42, username="fixture")], tokens=["fresh"])
    clients = iter([expired, fresh])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: next(clients),  # type: ignore[arg-type]
        token_store=cache,  # type: ignore[arg-type]
    )
    assert await session.ensure_client() is fresh
    assert cache.deleted == []
    assert [record.token for record in cache.saved] == ["fresh"]
    assert cache.releases == [9]


@pytest.mark.asyncio
async def test_invalid_cached_token_is_atomically_replaced_after_fresh_login(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        token=None,
        login="login",
        password="password",
        expected_user_id=42,
        persist_token=True,
    )
    cache = CacheSpy(TokenRecord.create(user_id=42, username="fixture", token="old"))
    expired = FakeClient([unauthorized()])
    fresh = FakeClient([Actor(id=42, username="fixture")], tokens=["new"])
    clients = iter([expired, fresh])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: next(clients),  # type: ignore[arg-type]
        token_store=cache,  # type: ignore[arg-type]
    )
    assert await session.ensure_client() is fresh
    assert cache.deleted == []
    assert cache.saved[-1].token == "new"


@pytest.mark.asyncio
async def test_relogin_clears_all_token_sources_and_suppresses_close_error(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        login="login",
        password="password",
        expected_user_id=42,
    )
    cache = CacheSpy()
    old = FakeClient(
        [Actor(id=42, username="fixture")],
        close_error=RuntimeError("close failed"),
    )
    fresh = FakeClient([Actor(id=42, username="fixture")], tokens=["new"])
    clients = iter([old, fresh])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: next(clients),  # type: ignore[arg-type]
        token_store=cache,  # type: ignore[arg-type]
    )
    await session.ensure_client()
    assert await session.relogin() is fresh
    assert cache.deleted == []
    assert old.closed == 1


@pytest.mark.asyncio
async def test_relogin_follower_reuses_replacement_client(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        login="login",
        password="password",
        expected_user_id=42,
    )
    current = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: current,  # type: ignore[arg-type]
    )
    assert await session.ensure_client() is current
    stale = FakeClient([Actor(id=42, username="stale")])
    assert await session.relogin(stale_client=stale) is current
    assert current.closed == 0


@pytest.mark.asyncio
async def test_persist_skips_client_without_token(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        expected_user_id=42,
        persist_token=True,
    )
    cache = CacheSpy()
    client = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
        token_store=cache,  # type: ignore[arg-type]
    )
    session._client = client  # type: ignore[assignment]
    session._actor = Actor(id=42, username="fixture")
    await session._persist_current_token_locked("account-42")
    assert cache.saved == []


@pytest.mark.asyncio
async def test_read_second_auth_failure_is_not_retried(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        login="login",
        password="password",
        expected_user_id=42,
        read_attempts=5,
    )
    first = FakeClient([Actor(id=42, username="fixture")])
    second = FakeClient([Actor(id=42, username="fixture")], tokens=["new"])
    clients = iter([first, second])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: next(clients),  # type: ignore[arg-type]
    )

    async def fail(_client: Any) -> None:
        raise unauthorized()

    with pytest.raises(GatewayError) as caught:
        await session.call_read("projects", fail)
    assert caught.value.code is ErrorCode.AUTH_EXPIRED


@pytest.mark.asyncio
async def test_read_retry_honors_retry_after_and_records_failure(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        read_attempts=2,
        retry_backoff_base=0.1,
        retry_backoff_max=0.1,
    )
    coordinator = RecordingCoordinator()
    client = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(
        config,
        coordinator,  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr("kwork_mcp.session.random.uniform", lambda _a, _b: 0.0)
    calls = 0

    async def operation(_client: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KworkHTTPException(
                "limited",
                status=429,
                response_json={"retry_after": 0.5},
            )
        return "ok"

    assert await session.call_read("projects", operation) == "ok"
    assert sleeps == [0.1]
    assert coordinator.failures[-1] == ("account-42", "projects", 0.5)
    assert coordinator.successes[-1] == ("account-42", "projects")


@pytest.mark.asyncio
async def test_write_success_and_retryable_failure_update_circuit(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    coordinator = RecordingCoordinator()
    client = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(
        config,
        coordinator,  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    assert (
        await session.call_write_step(
            "write-delete-offer",
            lambda _client: asyncio.sleep(0, result="done"),
        )
        == "done"
    )
    assert coordinator.successes[-1] == ("account-42", "deleteOffer")

    async def fail(_client: Any) -> None:
        raise TimeoutError

    with pytest.raises(GatewayError):
        await session.call_write_step("write-delete-offer", fail)
    assert coordinator.failures[-1][:2] == ("account-42", "deleteOffer")


@pytest.mark.asyncio
async def test_web_login_transient_failure_and_none_status_are_classified(
    config_factory: Callable[..., KworkConfig],
) -> None:
    for web_result, code in [
        (TimeoutError(), ErrorCode.TIMEOUT),
        (SimpleNamespace(status=None), ErrorCode.AUTH_EXPIRED),
    ]:
        config = config_factory()
        coordinator = RecordingCoordinator()
        client = FakeClient(
            [Actor(id=42, username="fixture")],
            web_result=web_result,
        )
        session = KworkSessionManager(
            config,
            coordinator,  # type: ignore[arg-type]
            client_factory=lambda _, bound=client: bound,  # type: ignore[arg-type]
        )
        with pytest.raises(GatewayError) as caught:
            await session.ensure_web_client()
        assert caught.value.code is code
        if code is ErrorCode.TIMEOUT:
            assert coordinator.failures[-1][:2] == (
                "account-42",
                "getWebAuthToken",
            )


@pytest.mark.asyncio
async def test_client_guard_waits_for_exclusive_owner(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    client = FakeClient([Actor(id=42, username="fixture")])
    session = KworkSessionManager(
        config,
        RecordingCoordinator(),  # type: ignore[arg-type]
        client_factory=lambda _: client,  # type: ignore[arg-type]
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    reader_completed = asyncio.Event()

    async def holder() -> None:
        async with session.exclusive_client():
            entered.set()
            await release.wait()

    async def reader() -> None:
        await entered.wait()
        await session.ensure_web_client()
        reader_completed.set()

    holder_task = asyncio.create_task(holder())
    reader_task = asyncio.create_task(reader())
    await entered.wait()
    await asyncio.sleep(0)
    assert not reader_completed.is_set()
    release.set()
    await asyncio.gather(holder_task, reader_task)
    assert reader_completed.is_set()
