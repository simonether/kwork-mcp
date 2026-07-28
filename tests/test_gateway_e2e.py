from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kwork.schema.actor import Actor
from kwork.schema.category import ParentCategory, SubCategory
from kwork.schema.connects import Connects

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.models import (
    ConnectsData,
    ErrorCode,
    ItemCollection,
    MessageRecord,
    OfferRecord,
    ProjectRecord,
    SubmitOfferRequest,
    WriteAction,
    WriteState,
)


def _assert_no_secret_reflection(rendered: str, secrets: tuple[str, ...]) -> None:
    if any(secret and secret in rendered for secret in secrets):
        pytest.fail("gateway output contains a protected value", pytrace=False)


class ReadSession:
    def __init__(self, client: Any, scope: str = "account-42") -> None:
        self.client = client
        self.scope = scope
        self.actor = Actor(id=int(scope.removeprefix("account-")), username="fixture")

    async def call_read(
        self,
        _route: str,
        operation: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        return await operation(self.client)

    async def call_read_scoped(
        self,
        route: str,
        operation: Callable[[Any], Awaitable[Any]],
        *,
        expected_scope: str | None = None,
    ) -> tuple[Any, str]:
        if expected_scope is not None and expected_scope != self.scope:
            raise GatewayError(ErrorCode.VALIDATION)
        return await self.call_read(route, operation), self.scope


class ProjectClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def projects(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        assert use_token is True
        self.calls.append(params)
        page = int(params["page"])
        if page == 1:
            return {
                "success": True,
                "response": [
                    {
                        "id": 101,
                        "title": "Первый",
                        "description": "Внешний текст",
                        "date_confirm": 1000,
                        "unknown_future_field": "preserved",
                    }
                ],
                "paging": {"page": 1, "limit": 1, "total": 2, "pages": 2},
                "connects": {"active": 7},
            }
        return {
            "success": True,
            "response": [
                {
                    "id": 102,
                    "title": "Второй",
                    "description": "Ещё внешний текст",
                    "date_confirm": 1001,
                }
            ],
            "paging": {"page": 2, "limit": 1, "total": 2, "pages": 2},
            "connects": {"active": 7},
        }


@pytest.mark.asyncio
async def test_discovery_all_preserves_data_and_uses_bound_cursor(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    client = ProjectClient()
    gateway = KworkGateway(
        config,
        CoordinationStore(config),
        ReadSession(client),  # type: ignore[arg-type]
    )
    first = await gateway.discover_projects(
        mode="all",
        category_ids=None,
        price_from=None,
        price_to=None,
        hiring_from=None,
        offers_from=None,
        offers_to=None,
        query=None,
        cursor=None,
    )
    assert first.mode == "all"
    assert first.projects.items[0].project_id == 101
    assert first.projects.items[0].raw["unknown_future_field"] == "preserved"
    assert first.projects.page is not None
    assert first.projects.page.high_watermark == "1000:101"
    assert first.projects.page.next_cursor is not None
    assert client.calls[0] == {"categories": "all", "page": 1}

    second = await gateway.discover_projects(
        mode="all",
        category_ids=None,
        price_from=None,
        price_to=None,
        hiring_from=None,
        offers_from=None,
        offers_to=None,
        query=None,
        cursor=first.projects.page.next_cursor,
    )
    assert [item.project_id for item in second.projects.items] == [102]
    assert second.projects.page is not None
    assert second.projects.page.next_cursor is None
    assert client.calls[1] == {"categories": "all", "page": 2}

    with pytest.raises(GatewayError) as mismatch:
        await gateway.discover_projects(
            mode="favorites",
            category_ids=None,
            price_from=None,
            price_to=None,
            hiring_from=None,
            offers_from=None,
            offers_to=None,
            query=None,
            cursor=first.projects.page.next_cursor,
        )
    assert mismatch.value.code is ErrorCode.VALIDATION

    other_gateway = KworkGateway(
        config,
        gateway.coordinator,
        ReadSession(client, scope="account-99"),  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as cross_scope:
        await other_gateway.discover_projects(
            mode="all",
            category_ids=None,
            price_from=None,
            price_to=None,
            hiring_from=None,
            offers_from=None,
            offers_to=None,
            query=None,
            cursor=first.projects.page.next_cursor,
        )
    assert cross_scope.value.code is ErrorCode.VALIDATION


class ReadMatrixClient:
    async def get_me(self) -> Actor:
        return Actor(id=42, username="fixture", description="external")

    async def get_connects(self) -> Connects:
        return Connects(active_connects=3, all_connects=10)

    async def get_user(self, user_id: int) -> Actor:
        return Actor(
            id=user_id,
            username="numeric-user",
            description="Публичное описание пользователя",
        )

    async def user_by_username(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": {"id": 52, "username": params["username"], "new": "field"},
        }

    async def user_search(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": [{"id": 53, "username": "found"}],
            "paging": {"page": params["page"], "limit": 20, "total": 1},
        }

    async def project(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": [
                {
                    "id": params["id"],
                    "title": "Project",
                    "description": "Description",
                    "status": "active",
                    "date_confirm": 100,
                }
            ],
        }

    async def exchange_info(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {"success": True, "response": {"enabled": True}}

    async def offers(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": [{"id": 71, "title": "Offer"}],
            "paging": {"page": params["page"], "limit": 20, "total": 1, "pages": 1},
        }

    async def offer(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": {
                "id": params["id"],
                "project": {"id": 101},
                "name": "Offer",
                "comment": "Full",
                "kwork_price": 5000,
                "kwork_duration": 5,
            },
        }

    async def worker_orders(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        assert params == {"filter": "all", "page": 2}
        return {
            "success": True,
            "response": {
                "orders": [
                    {
                        "id": 81,
                        "status": 1,
                        "display_title": "Order",
                        "payer": {"id": 82, "username": "buyer"},
                    }
                ],
                "paging": {"page": 2, "limit": 20, "total": 21, "pages": 2},
            },
        }

    async def get_order_details(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": {"id": params["orderId"], "details": "kept"},
        }

    async def dialogs(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        assert use_token is True
        return {
            "success": True,
            "response": [
                {
                    "user_id": 91,
                    "username": "dialog-user",
                    "unread_count": 2,
                    "status": "online",
                }
            ],
            "paging": {"page": params["page"], "limit": 20, "total": 1, "pages": 1},
        }

    async def inboxes(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        assert use_token is True
        return {
            "success": True,
            "response": [
                {
                    "message_id": 92,
                    "from_id": 91,
                    "from_username": params["username"],
                    "message": "Hello",
                    "time": 1000,
                }
            ],
            "paging": {"page": params["page"], "limit": 20, "total": 1, "pages": 1},
        }

    async def kworks_status_list(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": [
                {
                    "id": 1,
                    "name": "Активные",
                    "kworks_count": 2,
                    "kworks": [{"id": 93, "title": "Service"}],
                }
            ],
        }

    async def user_kworks(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        assert use_token is True
        assert params["user_id"] == 42
        assert params["status_id"] == 1
        page = params["page"]
        kwork_id = 93 if page == 1 else 95
        return {
            "success": True,
            "response": [
                {
                    "id": kwork_id,
                    "title": f"Service {page}",
                    "status_id": 1,
                    "future_field": "preserved",
                }
            ],
            "paging": {"page": page, "limit": 1, "total": 2, "pages": 2},
        }

    async def get_kwork_details_extra(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {"success": True, "response": {"id": params["id"], "extra": True}}

    async def get_categories(self) -> list[ParentCategory]:
        return [
            ParentCategory(
                id=1,
                name="Root",
                description="Описание корневой категории",
                subcategories=[
                    SubCategory(
                        id=2,
                        name="Child",
                        description="Описание дочерней категории",
                    )
                ],
            )
        ]

    async def favorite_categories(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {"success": True, "response": [{"id": 2}]}

    async def get_notifications(self) -> dict[str, Any]:
        return {"success": True, "response": [{"id": 94, "text": "external"}]}


@pytest.mark.asyncio
async def test_read_matrix_normalizes_ids_and_preserves_raw_fields(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(expected_user_id=42)
    gateway = KworkGateway(
        config,
        CoordinationStore(config),
        ReadSession(ReadMatrixClient()),  # type: ignore[arg-type]
    )
    account = await gateway.account_status()
    assert account.user_id == 42
    assert account.binding_state == "bound"
    assert account.raw["description"] == "external"
    assert (await gateway.get_connects()).active == 3
    numeric_user = await gateway.get_user(user_id=51, username=None)
    assert numeric_user is not None and numeric_user.user_id == 51
    assert numeric_user.raw["description"] == "Публичное описание пользователя"
    by_name = await gateway.get_user(user_id=None, username="@named")
    assert by_name is not None and by_name.user_id == 52
    assert by_name.raw["new"] == "field"
    assert (await gateway.search_users("find", 3)).items[0].user_id == 53
    assert (await gateway.get_project(61)).project_id == 61  # type: ignore[union-attr]
    assert (await gateway.get_exchange_info()).raw == {"enabled": True}

    offers = await gateway.list_my_offers(1)
    assert offers.items[0].offer_id == 71
    assert offers.items[0].project_id == 101
    assert (await gateway.get_offer(72)).project_id == 101  # type: ignore[union-attr]
    orders = await gateway.list_worker_orders(2)
    assert orders.items[0].buyer_username == "buyer"
    assert (await gateway.get_order_details(81)).raw["details"] == "kept"  # type: ignore[union-attr]
    assert (await gateway.list_dialogs(1)).items[0].unread_count == 2
    assert (await gateway.get_dialog("dialog-user", 1)).items[0].message_id == 92
    kworks = await gateway.list_my_kworks()
    assert [item.kwork_id for item in kworks.items] == [93, 95]
    assert kworks.items[1].raw["future_field"] == "preserved"
    assert (await gateway.get_kwork_details(93)).raw["extra"] is True  # type: ignore[union-attr]
    categories = await gateway.list_categories()
    assert categories.items[0].children[0].category_id == 2
    assert categories.items[0].raw["description"] == "Описание корневой категории"
    assert categories.items[0].children[0].raw["description"] == "Описание дочерней категории"
    assert (await gateway.list_favorite_categories()).raw == [{"id": 2}]
    assert (await gateway.list_notifications()).raw[0]["id"] == 94  # type: ignore[index]


@pytest.mark.asyncio
async def test_missing_connect_balances_fail_as_contract_drift(
    config_factory: Callable[..., KworkConfig],
) -> None:
    class MissingConnectsClient:
        async def get_connects(self) -> Connects:
            return Connects(active_connects=None, all_connects=None)

    config = config_factory()
    gateway = KworkGateway(
        config,
        CoordinationStore(config),
        ReadSession(MissingConnectsClient()),  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as caught:
        await gateway.get_connects()
    assert caught.value.code is ErrorCode.CONTRACT_DRIFT
    assert caught.value.diagnostic == "connects:missing_balance_fields"


@pytest.mark.asyncio
async def test_reflected_credentials_are_removed_from_external_fields(
    config_factory: Callable[..., KworkConfig],
) -> None:
    proxy = "socks5://alice:p%2f%3aword@proxy.example"
    reflected_values = (
        "fixture-token",
        proxy,
        "socks5://alice:p%2F%3aword@proxy.example",
        "socks5://alice:p/:word@proxy.example",
        "alice:p%2f%3Aword",
        "alice:p/:word",
        "alice",
        "p%2F%3aword",
        "p/:word",
    )

    class ReflectedSecretClient:
        async def get_me(self) -> Actor:
            return Actor(
                id=42,
                username="fixture",
                description=" ".join(reflected_values),
            )

    config = config_factory(
        proxy_url=proxy,
        expected_user_id=42,
    )
    gateway = KworkGateway(
        config,
        CoordinationStore(config),
        ReadSession(ReflectedSecretClient()),  # type: ignore[arg-type]
    )
    account = await gateway.account_status()
    description = str(account.raw["description"])
    _assert_no_secret_reflection(description, reflected_values)
    assert "<redacted>" in description


class WriteSession:
    scope = "account-42"

    def __init__(self, identities: list[int] | None = None) -> None:
        self.identities = list(identities or [42])

    def _identity(self) -> Actor:
        value = self.identities.pop(0) if len(self.identities) > 1 else self.identities[0]
        return Actor(id=value, username=f"user-{value}")

    @asynccontextmanager
    async def exclusive_client(self) -> Any:
        yield

    async def verify_account_identity(self) -> Actor:
        return self._identity()

    async def verify_write_identity(self) -> Actor:
        return self._identity()


class OfferProtocolGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        *,
        execution: str = "success",
        existing_offers: list[OfferRecord] | None = None,
        session: WriteSession | None = None,
    ) -> None:
        super().__init__(
            config,
            coordinator,
            session or WriteSession(),  # type: ignore[arg-type]
        )
        self.execution = execution
        self.offers = list(existing_offers or [])
        self.write_calls = 0

    async def get_project(self, project_id: int) -> ProjectRecord | None:
        return ProjectRecord(
            project_id=project_id,
            title="Project",
            status="active",
            raw={"id": project_id, "status": "active"},
        )

    async def _all_offers(self, max_pages: int = 50) -> list[OfferRecord]:
        return list(self.offers)

    async def get_connects(self) -> ConnectsData:
        return ConnectsData(active=5, total=10, raw={"active": 5, "total": 10})

    async def _execute_write(
        self,
        record: Any,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        await before_remote_attempt()
        self.write_calls += 1
        request, _ = self._decode_request(record)
        created = OfferRecord(
            offer_id=9001,
            project_id=int(request["project_id"]),
            title=str(request["title"]),
            description=str(request["description"]),
            price=int(request["price"]),
            duration_days=int(request["duration_days"]),
            created_at=int(time.time()),
            raw={"id": 9001, "project_id": int(request["project_id"])},
        )
        if self.execution == "timeout_with_side_effect":
            self.offers.append(created)
            raise GatewayError(
                ErrorCode.TIMEOUT,
                retryable=True,
                safe_to_retry=True,
                diagnostic="fake_timeout",
            )
        if self.execution == "timeout_without_side_effect":
            raise GatewayError(
                ErrorCode.TIMEOUT,
                retryable=True,
                safe_to_retry=True,
                diagnostic="fake_timeout",
            )
        self.offers.append(created)
        return {"offer_id": created.offer_id, "project_id": created.project_id}


def offer_request(project_id: int = 77) -> SubmitOfferRequest:
    return SubmitOfferRequest(
        action=WriteAction.SUBMIT_OFFER,
        project_id=project_id,
        title="Реализация API",
        description="Д" * 180,
        price=5000,
        duration_days=7,
    )


@pytest.mark.asyncio
async def test_prepare_commit_submit_is_exactly_once_and_replay_safe(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    gateway = OfferProtocolGateway(config, CoordinationStore(config))
    prepared = await gateway.prepare_write(
        offer_request(),
        "offer-project-77",
        correlation_id="prepare",
    )
    assert prepared.state is WriteState.PREPARED
    assert prepared.can_commit is True
    assert prepared.confirmation_token is not None
    assert prepared.payload["request"]["project_id"] == 77  # type: ignore[index]

    committed = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit",
    )
    assert committed.state is WriteState.SUCCEEDED
    assert committed.result == {"offer_id": 9001, "project_id": 77}
    replay = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="replay",
    )
    assert replay.state is WriteState.SUCCEEDED
    assert gateway.write_calls == 1


@pytest.mark.asyncio
async def test_duplicate_offer_is_rejected_at_prepare(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    existing = OfferRecord(
        offer_id=1,
        project_id=77,
        raw={"id": 1, "project_id": 77},
    )
    gateway = OfferProtocolGateway(
        config,
        CoordinationStore(config),
        existing_offers=[existing],
    )
    with pytest.raises(GatewayError) as caught:
        await gateway.prepare_write(
            offer_request(),
            "duplicate-offer-77",
            correlation_id="prepare",
        )
    assert caught.value.code is ErrorCode.DUPLICATE
    assert gateway.write_calls == 0


@pytest.mark.asyncio
async def test_timeout_becomes_submission_unknown_then_readback_reconciles(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    gateway = OfferProtocolGateway(
        config,
        CoordinationStore(config),
        execution="timeout_with_side_effect",
    )
    prepared = await gateway.prepare_write(
        offer_request(),
        "ambiguous-offer-77",
        correlation_id="prepare",
    )
    assert prepared.confirmation_token is not None
    unknown = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit",
    )
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN
    assert unknown.reconciliation_required is True
    assert unknown.terminal_error is not None
    assert unknown.terminal_error.code is ErrorCode.AMBIGUOUS_WRITE

    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    reconciled = await gateway.reconcile_write(
        prepared.write_id,
        correlation_id="reconcile",
    )
    assert reconciled is not None
    assert reconciled.state is WriteState.RECONCILED_SUCCEEDED
    assert reconciled.result == {
        "offer_id": 9001,
        "project_id": 77,
        "reconciled": True,
    }
    assert gateway.write_calls == 1


@pytest.mark.asyncio
async def test_prepared_account_mismatch_releases_claim_without_remote_write(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    gateway = OfferProtocolGateway(
        config,
        CoordinationStore(config),
        session=WriteSession([42, 99]),
    )
    prepared = await gateway.prepare_write(
        offer_request(),
        "account-change-offer",
        correlation_id="prepare",
    )
    assert prepared.confirmation_token
    with pytest.raises(GatewayError) as mismatch:
        await gateway.commit_write(
            write_id=prepared.write_id,
            payload_hash=prepared.payload_hash,
            confirmation_token=prepared.confirmation_token,
            correlation_id="commit",
        )
    assert mismatch.value.code is ErrorCode.ACCOUNT_MISMATCH
    persisted = await gateway.get_write_status(prepared.write_id, correlation_id="status")
    assert persisted is not None and persisted.state is WriteState.PREPARED
    assert gateway.write_calls == 0


@pytest.mark.asyncio
async def test_gateway_requires_two_absent_readbacks_before_terminal_absence(
    config_factory: Callable[..., KworkConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = config_factory(
        enable_writes=True,
        expected_user_id=42,
        reconciliation_absence_interval_seconds=1.0,
    )
    gateway = OfferProtocolGateway(
        config,
        CoordinationStore(config),
        execution="timeout_without_side_effect",
    )
    prepared = await gateway.prepare_write(
        offer_request(),
        "absent-offer",
        correlation_id="prepare",
    )
    assert prepared.confirmation_token
    unknown = await gateway.commit_write(
        write_id=prepared.write_id,
        payload_hash=prepared.payload_hash,
        confirmation_token=prepared.confirmation_token,
        correlation_id="commit",
    )
    assert unknown.state is WriteState.SUBMISSION_UNKNOWN
    now = time.time()
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 1.1)
    first = await gateway.reconcile_write(prepared.write_id, correlation_id="first")
    assert first is not None and first.state is WriteState.SUBMISSION_UNKNOWN
    monkeypatch.setattr("kwork_mcp.coordination.time.time", lambda: now + 2.2)
    second = await gateway.reconcile_write(prepared.write_id, correlation_id="second")
    assert second is not None and second.state is WriteState.RECONCILED_ABSENT


class MessageReadbackGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        messages: list[MessageRecord],
    ) -> None:
        super().__init__(config, coordinator, WriteSession())  # type: ignore[arg-type]
        self.messages = messages

    async def get_dialog(
        self,
        username: str,
        page: int = 1,
    ) -> ItemCollection[MessageRecord]:
        return ItemCollection[MessageRecord](items=self.messages)


@pytest.mark.asyncio
async def test_string_timestamp_old_message_is_rejected_and_invalid_is_inconclusive(
    config_factory: Callable[..., KworkConfig],
) -> None:
    now = time.time()
    config = config_factory(expected_user_id=42)
    old = MessageReadbackGateway(
        config,
        CoordinationStore(config),
        [
            MessageRecord(
                message_id=1,
                sender_id=42,
                text="same text",
                created_at="2000-01-01T00:00:00Z",
                raw={"id": 1},
            )
        ],
    )
    matches = await old._matching_sent_messages(
        username="recipient",
        text="same text",
        sender_id=42,
        prepared_at=now,
        unknown_at=now,
    )
    assert matches == []

    invalid = MessageReadbackGateway(
        config,
        CoordinationStore(config),
        [
            MessageRecord(
                message_id=2,
                sender_id=42,
                text="same text",
                created_at="not-a-timestamp",
                raw={"id": 2},
            )
        ],
    )
    with pytest.raises(GatewayError) as inconclusive:
        await invalid._matching_sent_messages(
            username="recipient",
            text="same text",
            sender_id=42,
            prepared_at=now,
            unknown_at=now,
        )
    assert inconclusive.value.code is ErrorCode.AMBIGUOUS_WRITE


class WriteDispatchClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def delete_offer(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("delete_offer", params))
        return {"success": True}

    async def send_message(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("send_message", params))
        return {"success": True, "response": {"id": 501}}

    async def inbox_edit(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("inbox_edit", params))
        return {"success": True}

    async def delete_message(self, message_id: int) -> dict[str, Any]:
        self.calls.append(("delete_message", {"message_id": message_id}))
        return {"success": True}

    async def inbox_read(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("inbox_read", params))
        return {"success": True}

    async def send_order_for_approval(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("send_order_for_approval", params))
        return {"success": True}

    async def start_kwork(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("start_kwork", params))
        return {"success": True}

    async def pause_kwork(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("pause_kwork", params))
        return {"success": True}


class WriteDispatchSession(WriteSession):
    def __init__(self, client: WriteDispatchClient) -> None:
        super().__init__()
        self.client = client

    async def call_write_step(
        self,
        route: str,
        operation: Callable[[Any], Awaitable[Any]],
        *,
        before_remote_attempt: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        if before_remote_attempt is not None:
            await before_remote_attempt()
        return await operation(self.client)


@pytest.mark.asyncio
async def test_all_non_offer_write_actions_use_exact_upstream_parameters(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    coordinator = CoordinationStore(config)
    client = WriteDispatchClient()
    gateway = KworkGateway(
        config,
        coordinator,
        WriteDispatchSession(client),  # type: ignore[arg-type]
    )
    cases: list[tuple[WriteAction, dict[str, Any], dict[str, Any], str, dict[str, Any]]] = [
        (
            WriteAction.DELETE_OFFER,
            {"action": "delete_offer", "offer_id": 11},
            {},
            "delete_offer",
            {"use_token": True, "retry": False, "id": 11},
        ),
        (
            WriteAction.SEND_MESSAGE,
            {"action": "send_message", "text": "hello"},
            {"user_id": 12},
            "send_message",
            {"user_id": 12, "text": "hello"},
        ),
        (
            WriteAction.EDIT_MESSAGE,
            {
                "action": "edit_message",
                "message_id": 13,
                "username": "user",
                "text": "edited",
            },
            {},
            "inbox_edit",
            {
                "use_token": True,
                "retry": False,
                "body": {"text": "edited"},
                "id": 13,
            },
        ),
        (
            WriteAction.DELETE_MESSAGE,
            {"action": "delete_message", "message_id": 14, "username": "user"},
            {},
            "delete_message",
            {"message_id": 14},
        ),
        (
            WriteAction.MARK_DIALOG_READ,
            {"action": "mark_dialog_read", "user_id": 15},
            {},
            "inbox_read",
            {"use_token": True, "retry": False, "user_id": 15},
        ),
        (
            WriteAction.SUBMIT_ORDER_APPROVAL,
            {
                "action": "submit_order_approval",
                "order_id": 16,
                "metrics": [1],
                "stage_ids": [2],
                "file_ids": [3],
            },
            {},
            "send_order_for_approval",
            {
                "use_token": True,
                "retry": False,
                "orderId": 16,
                "metrics[]": [1],
                "stageIds[]": [2],
                "filesIds[]": [3],
            },
        ),
        (
            WriteAction.SET_KWORK_STATE,
            {"action": "set_kwork_state", "kwork_id": 17, "target_state": "active"},
            {},
            "start_kwork",
            {"use_token": True, "retry": False, "kwork_id": 17},
        ),
        (
            WriteAction.SET_KWORK_STATE,
            {"action": "set_kwork_state", "kwork_id": 18, "target_state": "paused"},
            {},
            "pause_kwork",
            {"use_token": True, "retry": False, "kwork_id": 18},
        ),
    ]
    for index, (action, request, resolved, expected_method, expected_params) in enumerate(cases):
        prepared = await coordinator.prepare_write(
            scope="account-42",
            idempotency_key=f"dispatch-{index}",
            action=action,
            payload={
                "request": request,
                "resolved": resolved,
                "prepared_account_id": 42,
            },
        )
        await gateway._execute_write(prepared.record)
        assert client.calls[-1] == (expected_method, expected_params)
