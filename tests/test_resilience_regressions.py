from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from kwork.exceptions import KworkHTTPException
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError, classify_upstream_error
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.models import ErrorCode
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.upstream import SecureKworkWebClient

_OFFER_HTML = "".join(
    (
        'csrf_user_token="',
        "0123456789abcdef",
        '" draftKey="resilient123"',
    )
)


class _OfferWeb:
    base_url = "https://kwork.ru/"

    def __init__(self, failed_step: str) -> None:
        self.failed_step = failed_step
        self.calls: list[str] = []

    def _response(self, step: str) -> dict[str, Any]:
        return {
            "status": 503 if step == self.failed_step else 200,
            "json": {"error": "temporary"} if step == self.failed_step else {},
        }

    async def open_new_offer_page(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("page")
        return {"status": 200, "text": _OFFER_HTML}

    async def quick_faq_init(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("faq")
        return self._response("faq")

    async def create_offer_draft(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("draft")
        return self._response("draft")

    async def check_is_template(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("template")
        return self._response("template")

    async def create_exchange_offer(self, **_params: Any) -> dict[str, Any]:
        self.calls.append("final")
        return {"status": 200, "json": {"success": True, "id": 9001}}


class _OfferSession:
    scope = "account-42"

    def __init__(self, web: _OfferWeb) -> None:
        empty_cookie_jar = SimpleNamespace(filter_cookies=lambda _url: {})
        self.client = SimpleNamespace(
            web=web,
            session=SimpleNamespace(cookie_jar=empty_cookie_jar),
        )

    async def ensure_web_client(self) -> Any:
        return self.client

    async def call_write_step(
        self,
        _route: str,
        operation: Callable[[Any], Awaitable[Any]],
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        if before_remote_attempt is not None:
            await before_remote_attempt()
        return await operation(self.client)


@pytest.mark.parametrize(
    ("failed_step", "expected_calls"),
    [
        ("faq", ["page", "faq"]),
        ("draft", ["page", "faq", "draft"]),
        ("template", ["page", "faq", "draft", "template"]),
    ],
)
@pytest.mark.asyncio
async def test_submit_offer_prerequisite_5xx_stops_before_final_create(
    config_factory: Callable[..., KworkConfig],
    failed_step: str,
    expected_calls: list[str],
) -> None:
    config = config_factory()
    web = _OfferWeb(failed_step)
    gateway = KworkGateway(
        config,
        CoordinationStore(config),
        _OfferSession(web),  # type: ignore[arg-type]
    )

    with pytest.raises(GatewayError) as caught:
        await gateway._execute_submit_offer(
            {
                "project_id": 42,
                "title": "Resilient offer",
                "description": "D" * 150,
                "price": 1000,
                "duration_days": 3,
            }
        )

    assert caught.value.code is ErrorCode.UPSTREAM_UNAVAILABLE
    assert web.calls == expected_calls
    assert "final" not in web.calls


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (901.0, 900.0),
        (10_000.0, 900.0),
    ],
)
def test_retry_after_non_finite_is_ignored_and_large_values_are_capped(
    hint: float,
    expected: float | None,
) -> None:
    error = classify_upstream_error(
        KworkHTTPException(
            "rate limited",
            status=429,
            response_json={"retry_after": hint},
        )
    )

    assert error.code is ErrorCode.RATE_LIMIT
    assert error.retry_after_seconds == expected
    assert error.retry_after_seconds is None or math.isfinite(error.retry_after_seconds)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (
            {
                "success": False,
                "error_code": 118,
                "message": "captcha: remote-secret-value",
            },
            ErrorCode.CAPTCHA,
        ),
        (
            {
                "success": False,
                "message": "opaque remote failure: remote-secret-value",
            },
            ErrorCode.PERMISSION,
        ),
    ],
)
def test_web_prerequisite_403_preserves_taxonomy_without_reflecting_remote_text(
    payload: dict[str, Any],
    expected_code: ErrorCode,
) -> None:
    with pytest.raises(KworkHTTPException) as upstream:
        SecureKworkWebClient._raise_on_web_error(
            {"status": 403, "json": payload},
            where="offer-prerequisite",
        )

    classified = classify_upstream_error(upstream.value)
    serialized = classified.to_info("safe-correlation-id").model_dump_json()

    assert classified.code is expected_code
    assert "remote-secret-value" not in str(classified)
    assert "remote-secret-value" not in repr(classified)
    assert "remote-secret-value" not in serialized
    assert "opaque remote failure" not in serialized


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.failures: list[float | None] = []

    async def acquire(self, _scope: str, _route: str) -> None:
        return None

    async def record_failure(
        self,
        _scope: str,
        _route: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.failures.append(retry_after_seconds)

    async def record_success(self, _scope: str, _route: str) -> None:
        return None


@pytest.mark.parametrize(
    ("hint", "expected_recorded", "expected_sleep"),
    [
        (float("nan"), None, 0.2),
        (float("inf"), None, 0.2),
        (10_000.0, 900.0, 0.3),
    ],
)
@pytest.mark.asyncio
async def test_read_retry_sleep_never_exceeds_configured_backoff_max(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
    hint: float,
    expected_recorded: float | None,
    expected_sleep: float,
) -> None:
    config = config_factory(
        read_attempts=2,
        retry_backoff_base=0.2,
        retry_backoff_max=0.3,
    )
    coordinator = _RecordingCoordinator()
    session = KworkSessionManager(
        config,
        coordinator,  # type: ignore[arg-type]
    )
    session._client = SimpleNamespace()  # type: ignore[assignment]
    session._actor = Actor(id=42, username="fixture")
    sleeps: list[float] = []

    async def capture_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", capture_sleep)
    monkeypatch.setattr("kwork_mcp.session.random.uniform", lambda _low, _high: 0.0)
    attempts = 0

    async def operation(_client: Any) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KworkHTTPException(
                "rate limited",
                status=429,
                response_json={"retry_after": hint},
            )
        return "ok"

    assert await session.call_read("projects", operation) == "ok"
    assert coordinator.failures == [expected_recorded]
    assert sleeps == [expected_sleep]
    assert max(sleeps) <= config.retry_backoff_max


@pytest.mark.asyncio
async def test_half_open_allows_exactly_one_probe_and_blocks_every_second_check(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        circuit_failure_threshold=1,
        circuit_open_seconds=1.0,
        timeout=1.0,
    )
    owner = CoordinationStore(config)
    other_process = CoordinationStore(config)
    now = time.time()

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    await owner.record_failure("account-42", "projects")
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)

    await owner.check_circuit("account-42", "projects")
    for blocked_store in (owner, other_process):
        with pytest.raises(GatewayError) as blocked:
            await blocked_store.check_circuit("account-42", "projects")

        assert blocked.value.code is ErrorCode.CIRCUIT_OPEN
        assert blocked.value.retry_after_seconds is not None
        assert blocked.value.retry_after_seconds > 0


class _AuthRefreshClient:
    def __init__(self) -> None:
        self._token: str | None = "stale-token"
        self.closed = False

    async def get_token(self) -> str:
        self._token = "refreshed-token"
        return self._token

    async def get_me(self) -> Actor:
        return Actor(id=42, username="fixture")

    async def close(self) -> None:
        self.closed = True


class _IdentityClient:
    def __init__(self, user_id: int, username: str) -> None:
        self.actor = Actor(id=user_id, username=username)
        self._token: str | None = None
        self.closed = False
        self.get_token_calls = 0

    async def get_token(self) -> str:
        self.get_token_calls += 1
        self._token = "refreshed-token"
        return self._token

    async def get_me(self) -> Actor:
        return self.actor

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_unbound_session_rejects_account_change_during_401_refresh(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(
        login="fixture-login",
        password="fixture-password",
    )
    account_a = _IdentityClient(42, "account-a")
    account_b = _IdentityClient(99, "account-b")
    clients = iter((account_a, account_b))
    session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _config: next(clients),  # type: ignore[arg-type]
    )
    operation_clients: list[_IdentityClient] = []

    async def operation(client: Any) -> str:
        operation_clients.append(client)
        if client is account_a:
            raise KworkHTTPException(
                "expired",
                status=401,
                response_json={"success": False},
            )
        return "private-data-from-account-b"

    with pytest.raises(GatewayError) as caught:
        await session.call_read("projects", operation)

    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    assert operation_clients == [account_a]
    assert account_a.closed is True
    assert account_b.get_token_calls == 1
    assert account_b.closed is True
    assert session.actor is None


@pytest.mark.asyncio
async def test_identity_mismatch_releases_successful_half_open_probe(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        expected_user_id=42,
        circuit_failure_threshold=1,
        circuit_open_seconds=1.0,
        timeout=1.0,
    )
    coordinator = CoordinationStore(config)
    client = _IdentityClient(99, "wrong-account")
    session = KworkSessionManager(config, coordinator)
    now = time.time()

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    await coordinator.record_failure("account-42", "auth-identity")
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)

    with pytest.raises(GatewayError) as caught:
        await session._validate_token_client(client)

    assert caught.value.code is ErrorCode.ACCOUNT_MISMATCH
    await coordinator.acquire("account-42", "auth-identity")


@pytest.mark.asyncio
async def test_half_open_auth_refresh_clears_probe_before_logical_read_retry(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        login="fixture",
        password="password",
        expected_user_id=42,
        circuit_failure_threshold=1,
        circuit_open_seconds=1.0,
        timeout=1.0,
    )
    coordinator = CoordinationStore(config)
    stale = _AuthRefreshClient()
    refreshed = _AuthRefreshClient()
    session = KworkSessionManager(
        config,
        coordinator,
        client_factory=lambda _config: refreshed,  # type: ignore[arg-type]
    )
    session._client = stale  # type: ignore[assignment]
    session._actor = Actor(id=42, username="fixture")
    now = time.time()

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    await coordinator.record_failure("account-42", "projects")
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    attempted_with: list[_AuthRefreshClient] = []

    async def operation(client: Any) -> str:
        attempted_with.append(client)
        if client is stale:
            raise KworkHTTPException(
                "expired",
                status=401,
                response_json={"success": False},
            )
        return "ok"

    assert await session.call_read("projects", operation) == "ok"
    assert attempted_with == [stale, refreshed]
    assert stale.closed is True
    await coordinator.check_circuit("account-42", "projects")


@pytest.mark.parametrize(
    ("call_kind", "route", "canonical_route"),
    [
        ("read", "projects", "projects"),
        ("write", "write-delete-offer", "deleteOffer"),
    ],
)
@pytest.mark.parametrize(
    ("status", "payload", "expected_code"),
    [
        (404, {"message": "missing"}, ErrorCode.NOT_FOUND),
        (403, {"message": "opaque rejection"}, ErrorCode.PERMISSION),
        (409, {"message": "opaque conflict"}, ErrorCode.DUPLICATE),
    ],
)
@pytest.mark.asyncio
async def test_definitive_response_releases_half_open_probe_for_next_acquire(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
    call_kind: str,
    route: str,
    canonical_route: str,
    status: int,
    payload: dict[str, Any],
    expected_code: ErrorCode,
) -> None:
    config = config_factory(
        circuit_failure_threshold=1,
        circuit_open_seconds=1.0,
        timeout=1.0,
    )
    coordinator = CoordinationStore(config)
    session = KworkSessionManager(config, coordinator)
    session._client = SimpleNamespace()  # type: ignore[assignment]
    session._actor = Actor(id=42, username="fixture")
    now = time.time()

    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now)
    await coordinator.record_failure("account-42", canonical_route)
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    operation_calls = 0

    async def definitive_failure(_client: Any) -> None:
        nonlocal operation_calls
        operation_calls += 1
        raise KworkHTTPException(
            "definitive response",
            status=status,
            response_json=payload,
        )

    with pytest.raises(GatewayError) as caught:
        if call_kind == "read":
            await session.call_read(route, definitive_failure)
        else:
            await session.call_write_step(route, definitive_failure)

    assert caught.value.code is expected_code
    assert operation_calls == 1
    await coordinator.acquire("account-42", canonical_route)


@pytest.mark.parametrize(
    ("status", "payload", "expected_code"),
    [
        (
            403,
            {"message": "insufficient connects to submit offer"},
            ErrorCode.INSUFFICIENT_CONNECTS,
        ),
        (
            403,
            {"message": "project is closed"},
            ErrorCode.CLOSED_PROJECT,
        ),
        (
            409,
            {"message": "project is closed"},
            ErrorCode.CLOSED_PROJECT,
        ),
        (
            403,
            {"message": "opaque rejection"},
            ErrorCode.PERMISSION,
        ),
        (
            409,
            {"message": "opaque conflict"},
            ErrorCode.DUPLICATE,
        ),
    ],
)
def test_business_error_payload_takes_precedence_over_generic_http_taxonomy(
    status: int,
    payload: dict[str, Any],
    expected_code: ErrorCode,
) -> None:
    classified = classify_upstream_error(
        KworkHTTPException(
            "upstream rejection",
            status=status,
            response_json=payload,
        )
    )

    assert classified.code is expected_code


class _DiscoveryClient:
    def __init__(self, project_id: int) -> None:
        self.project_id = project_id
        self.calls: list[int] = []

    async def projects(
        self,
        *,
        use_token: bool,
        **params: Any,
    ) -> dict[str, Any]:
        assert use_token is True
        page = int(params["page"])
        self.calls.append(page)
        return {
            "success": True,
            "response": [
                {
                    "id": self.project_id,
                    "title": "Project",
                    "description": "External text",
                    "date_confirm": 1000,
                }
            ],
            "paging": {"page": page, "limit": 1, "total": 2, "pages": 2},
        }


class _AtomicDiscoverySession:
    def __init__(self) -> None:
        self.current_scope = "account-42"
        self.clients = {
            "account-42": _DiscoveryClient(4201),
            "account-99": _DiscoveryClient(9901),
        }
        self.rotate_after_first_read = True

    @property
    def scope(self) -> str:
        return self.current_scope

    async def call_read_scoped(
        self,
        _route: str,
        operation: Callable[[Any], Awaitable[Any]],
        *,
        expected_scope: str | None = None,
    ) -> tuple[Any, str]:
        authenticated_scope = self.current_scope
        if expected_scope is not None and authenticated_scope != expected_scope:
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic="authenticated_scope_mismatch",
            )
        result = await operation(self.clients[authenticated_scope])
        if self.rotate_after_first_read:
            self.rotate_after_first_read = False
            self.current_scope = "account-99"
        return result, authenticated_scope


async def _discover_all_projects(
    gateway: KworkGateway,
    *,
    cursor: str | None = None,
) -> Any:
    return await gateway.discover_projects(
        mode="all",
        category_ids=None,
        price_from=None,
        price_to=None,
        hiring_from=None,
        offers_from=None,
        offers_to=None,
        query=None,
        cursor=cursor,
    )


@pytest.mark.asyncio
async def test_discovery_cursor_scope_and_remote_read_are_atomic(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    coordinator = CoordinationStore(config)
    session = _AtomicDiscoverySession()
    gateway = KworkGateway(
        config,
        coordinator,
        session,  # type: ignore[arg-type]
    )

    first_page = await _discover_all_projects(gateway)
    cursor = first_page.projects.page.next_cursor
    assert cursor is not None
    decoded = await gateway.cursor_codec.decode(cursor)
    assert decoded["scope"] == "account-42"
    assert session.scope == "account-99"

    with pytest.raises(GatewayError) as caught:
        await _discover_all_projects(gateway, cursor=cursor)

    assert caught.value.code is ErrorCode.VALIDATION
    assert session.clients["account-42"].calls == [1]
    assert session.clients["account-99"].calls == []


@pytest.mark.asyncio
async def test_shared_state_requires_identical_coordination_policy(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    first = CoordinationStore(config)
    same_policy = CoordinationStore(config.model_copy())

    first_secret = await first.get_or_create_secret("shared-policy-fixture")
    assert await same_policy.get_or_create_secret("shared-policy-fixture") == first_secret

    incompatible = config.model_copy(
        update={"rps_limit": config.rps_limit / 2},
    )
    with pytest.raises(GatewayError) as caught:
        CoordinationStore(incompatible)

    assert caught.value.code is ErrorCode.CONTRACT_DRIFT
    assert caught.value.diagnostic == "coordination_policy_mismatch"
