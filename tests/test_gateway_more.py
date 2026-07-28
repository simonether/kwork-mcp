from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from kwork.schema.actor import Actor
from kwork.schema.category import ParentCategory

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore, StoredWrite
from kwork_mcp.errors import AmbiguousWriteError, GatewayError
from kwork_mcp.gateway import (
    KworkGateway,
    _as_json_dict,
    _as_json_list,
    _epoch_seconds,
    _optional_int,
    _optional_number,
    _optional_scalar,
    _positive_int,
    _response,
)
from kwork_mcp.models import (
    ConnectsData,
    DeleteMessageRequest,
    DeleteOfferRequest,
    DialogRecord,
    EditMessageRequest,
    ErrorCode,
    ItemCollection,
    KworkRecord,
    MarkDialogReadRequest,
    MessageRecord,
    OfferRecord,
    OrderRecord,
    PageInfo,
    ProjectRecord,
    SendMessageRequest,
    SetKworkStateRequest,
    SubmitOfferRequest,
    SubmitOrderApprovalRequest,
    UserRecord,
    WriteAction,
    WriteState,
)


class ScriptClient:
    def __init__(self, **responses: Any) -> None:
        self.responses = responses
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        async def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            value = self.responses[name]
            if isinstance(value, BaseException):
                raise value
            if callable(value):
                return value(*args, **kwargs)
            return value

        return call


class DirectSession:
    scope = "account-42"

    def __init__(self, client: Any) -> None:
        self.client = client
        self.actor = Actor(id=42, username="fixture")

    async def call_read(
        self,
        route: str,
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


def gateway_for(config: KworkConfig, client: Any) -> KworkGateway:
    return KworkGateway(
        config,
        CoordinationStore(config),
        DirectSession(client),  # type: ignore[arg-type]
    )


def test_gateway_scalar_and_envelope_helpers_cover_strict_edges() -> None:
    assert _positive_int(True) is None
    assert _positive_int(-1) is None
    assert _positive_int("12") == 12
    assert _positive_int("x") is None
    assert _optional_int(True) is None
    assert _optional_int(-3) == -3
    assert _optional_int("-4") == -4
    assert _optional_int(1.5) is None
    assert _optional_number(False) is None
    assert _optional_number(1.5) == 1.5
    assert _optional_scalar(False) is None
    assert _optional_scalar("ok") == "ok"
    assert _epoch_seconds(None) is None
    assert _epoch_seconds(100) == 100.0
    assert _epoch_seconds("100.5") == 100.5
    assert _epoch_seconds("2026-01-01T00:00:00") is not None
    assert _epoch_seconds("invalid") is None
    assert _as_json_dict({"x": 1}, "route") == {"x": 1}
    assert _as_json_list([1], "route") == [1]
    with pytest.raises(GatewayError):
        _as_json_dict([], "route")
    with pytest.raises(GatewayError):
        _as_json_list({}, "route")
    assert _response({"success": True}, "route", require_response=False) is None
    with pytest.raises(GatewayError):
        _response([], "route")
    with pytest.raises(GatewayError):
        _response({"success": True}, "route")
    with pytest.raises(GatewayError):
        _response({"success": False, "error": "closed"}, "route")


@pytest.mark.asyncio
async def test_read_identity_user_search_and_discovery_error_branches(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    gateway = gateway_for(
        config,
        ScriptClient(
            get_me=Actor(),
            get_user=Actor(),
            user_by_username={"success": True, "response": []},
            user_search={"success": True, "response": {"users": "bad"}},
            projects={"success": True, "response": {}, "paging": {}},
        ),
    )
    with pytest.raises(GatewayError):
        await gateway.account_status()
    with pytest.raises(GatewayError):
        await gateway.get_user(user_id=None, username=None)
    with pytest.raises(GatewayError):
        await gateway.get_user(user_id=1, username="both")
    with pytest.raises(GatewayError):
        await gateway.get_user(user_id=1, username=None)
    with pytest.raises(GatewayError):
        await gateway.get_user(user_id=None, username="@ ")
    assert await gateway.get_user(user_id=None, username="missing") is None
    with pytest.raises(GatewayError):
        await gateway.search_users("query", 1)

    base = {
        "category_ids": None,
        "price_from": None,
        "price_to": None,
        "hiring_from": None,
        "offers_from": None,
        "offers_to": None,
        "query": None,
        "cursor": None,
    }
    invalid_cases = [
        {"mode": "category_ids", **base},
        {"mode": "all", **base, "category_ids": [1]},
        {"mode": "all", **base, "price_from": 2, "price_to": 1},
        {"mode": "all", **base, "offers_from": 2, "offers_to": 1},
        {"mode": "category_ids", **base, "category_ids": list(range(1, 102))},
    ]
    for arguments in invalid_cases:
        with pytest.raises(GatewayError):
            await gateway.discover_projects(**arguments)  # type: ignore[arg-type]
    with pytest.raises(GatewayError):
        await gateway.discover_projects(mode="all", **base)

    gateway.session.client.responses["projects"] = {
        "success": True,
        "response": [],
    }
    with pytest.raises(GatewayError):
        await gateway.discover_projects(mode="favorites", **base)


@pytest.mark.asyncio
async def test_user_not_found_and_falsy_collection_shapes_are_fail_loud(
    config_factory: Callable[..., KworkConfig],
) -> None:
    missing_user = gateway_for(
        config_factory(),
        ScriptClient(get_user=GatewayError(ErrorCode.NOT_FOUND)),
    )
    assert await missing_user.get_user(user_id=404, username=None) is None

    bad_users = gateway_for(
        config_factory(),
        ScriptClient(user_search={"success": True, "response": {"users": ""}}),
    )
    with pytest.raises(GatewayError) as users_drift:
        await bad_users.search_users("query", 1)
    assert users_drift.value.code is ErrorCode.CONTRACT_DRIFT

    bad_offers = gateway_for(
        config_factory(),
        ScriptClient(
            offers={
                "success": True,
                "response": {"offers": {}},
                "paging": {"page": 1, "limit": 20, "total": 0, "pages": 0},
            }
        ),
    )
    with pytest.raises(GatewayError) as offers_drift:
        await bad_offers.list_my_offers()
    assert offers_drift.value.code is ErrorCode.CONTRACT_DRIFT

    for method_name, response in (
        (
            "list_dialogs",
            {
                "dialogs": {
                    "success": True,
                    "response": {},
                    "paging": {"page": 1, "limit": 20, "total": 0, "pages": 0},
                }
            },
        ),
        (
            "get_dialog",
            {
                "inboxes": {
                    "success": True,
                    "response": "",
                    "paging": {"page": 1, "limit": 20, "total": 0, "pages": 0},
                }
            },
        ),
    ):
        gateway = gateway_for(config_factory(), ScriptClient(**response))
        with pytest.raises(GatewayError) as dialog_drift:
            if method_name == "list_dialogs":
                await gateway.list_dialogs()
            else:
                await gateway.get_dialog("fixture")
        assert dialog_drift.value.code is ErrorCode.CONTRACT_DRIFT

    for method_name, response in (
        ("list_dialogs", {"dialogs": {"success": True, "response": [], "paging": {}}}),
        ("get_dialog", {"inboxes": {"success": True, "response": [], "paging": {}}}),
    ):
        gateway = gateway_for(config_factory(), ScriptClient(**response))
        with pytest.raises(GatewayError) as paging_drift:
            if method_name == "list_dialogs":
                await gateway.list_dialogs()
            else:
                await gateway.get_dialog("fixture")
        assert paging_drift.value.code is ErrorCode.CONTRACT_DRIFT


@pytest.mark.asyncio
async def test_safety_critical_paging_requires_exact_integers_and_requested_page(
    config_factory: Callable[..., KworkConfig],
) -> None:
    malformed_calls = (
        (
            gateway_for(
                config_factory(),
                ScriptClient(
                    offers={
                        "success": True,
                        "response": [],
                        "paging": {"page": 1, "pages": 1.5},
                    }
                ),
            ).list_my_offers(),
            "offers:paging_pages_invalid",
        ),
        (
            gateway_for(
                config_factory(),
                ScriptClient(
                    worker_orders={
                        "success": True,
                        "response": {
                            "orders": [],
                            "paging": {
                                "page": 1,
                                "limit": 20.5,
                                "total": 0,
                                "pages": 0,
                            },
                        },
                    }
                ),
            ).list_worker_orders(),
            "workerOrders:paging_limit_invalid",
        ),
        (
            gateway_for(
                config_factory(),
                ScriptClient(
                    dialogs={
                        "success": True,
                        "response": [],
                        "paging": {"page": 1, "total": 0.5, "pages": 0},
                    }
                ),
            ).list_dialogs(),
            "dialogs:paging_total_invalid",
        ),
    )
    for call, diagnostic in malformed_calls:
        with pytest.raises(GatewayError) as malformed:
            await call
        assert malformed.value.code is ErrorCode.CONTRACT_DRIFT
        assert malformed.value.diagnostic == diagnostic

    class RepeatingFirstPageClient:
        async def projects(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
            assert use_token is True
            return {
                "success": True,
                "response": [{"id": 1, "date_confirm": 100}],
                "paging": {"page": 1, "limit": 1, "total": 2, "pages": 2},
            }

    config = config_factory()
    gateway = gateway_for(config, RepeatingFirstPageClient())
    arguments: dict[str, Any] = {
        "mode": "all",
        "category_ids": None,
        "price_from": None,
        "price_to": None,
        "hiring_from": None,
        "offers_from": None,
        "offers_to": None,
        "query": None,
        "cursor": None,
    }
    first = await gateway.discover_projects(**arguments)
    assert first.projects.page is not None
    assert first.projects.page.next_cursor is not None
    arguments["cursor"] = first.projects.page.next_cursor
    with pytest.raises(GatewayError) as repeated:
        await gateway.discover_projects(**arguments)
    assert repeated.value.code is ErrorCode.CONTRACT_DRIFT
    assert repeated.value.diagnostic == "projects:paging_page_mismatch"


@pytest.mark.asyncio
async def test_read_details_and_collection_contract_drift_branches(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    not_found = GatewayError(ErrorCode.NOT_FOUND)
    gateway = gateway_for(
        config,
        ScriptClient(
            project=not_found,
            exchange_info={"success": True, "response": "bad"},
            offer=not_found,
            offers={"success": True, "response": "bad", "paging": {}},
            worker_orders={"success": True, "response": []},
            get_order_details=not_found,
            dialogs={
                "success": True,
                "response": [{"user_id": 1}],
                "paging": {"page": 1, "limit": 20, "total": 1, "pages": 1},
            },
            inboxes={"success": True, "response": [], "paging": {}},
            kworks_status_list={"success": True, "response": {}},
            get_kwork_details_extra=not_found,
            get_categories=[ParentCategory(id=None)],
            favorite_categories={"success": True, "response": "bad"},
            get_notifications={"success": True, "response": "bad"},
        ),
    )
    assert await gateway.get_project(1) is None
    with pytest.raises(GatewayError):
        await gateway.get_exchange_info()
    assert await gateway.get_offer(1) is None
    with pytest.raises(GatewayError):
        await gateway.list_my_offers()
    with pytest.raises(GatewayError):
        await gateway.list_worker_orders()
    assert await gateway.get_order_details(1) is None
    with pytest.raises(GatewayError):
        await gateway.list_dialogs()
    with pytest.raises(GatewayError):
        await gateway.get_dialog("@ ")
    with pytest.raises(GatewayError):
        await gateway.list_my_kworks()
    assert await gateway.get_kwork_details(1) is None
    with pytest.raises(GatewayError):
        await gateway.list_categories()
    with pytest.raises(GatewayError):
        await gateway.list_favorite_categories()
    with pytest.raises(GatewayError):
        await gateway.list_notifications()


@pytest.mark.asyncio
async def test_read_record_count_missing_ids_and_paging_contracts(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    client = ScriptClient(
        project={"success": True, "response": [{"id": 1}, {"id": 2}]},
        offer={"success": True, "response": [{"id": 1}, {"id": 2}]},
        offers={"success": True, "response": [{"id": 1}], "paging": {}},
        worker_orders={
            "success": True,
            "response": {"orders": [{"id": None}], "paging": {}},
        },
    )
    gateway = gateway_for(config, client)
    with pytest.raises(GatewayError):
        await gateway.get_project(1)
    with pytest.raises(GatewayError):
        await gateway.get_offer(1)
    client.responses["offer"] = {"success": True, "response": {"id": 1}}
    with pytest.raises(GatewayError):
        await gateway.get_offer(1)
    with pytest.raises(GatewayError):
        await gateway.list_my_offers()
    with pytest.raises(GatewayError):
        await gateway.list_worker_orders()

    client.responses["offers"] = {
        "success": True,
        "response": [],
    }
    with pytest.raises(GatewayError):
        await gateway.list_my_offers()
    client.responses["worker_orders"] = {
        "success": True,
        "response": {"orders": []},
    }
    with pytest.raises(GatewayError):
        await gateway.list_worker_orders()


@pytest.mark.asyncio
async def test_read_wire_shape_variants_and_watermark(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    client = ScriptClient(
        user_by_username={"success": True, "response": {"username": "missing-id"}},
        user_search={
            "success": True,
            "response": {"items": [{"username": "missing-id"}]},
        },
        projects={
            "success": True,
            "response": [
                {"id": 1, "title": "older", "date_confirm": "100.25"},
                {"id": 2, "title": "newer", "date_confirm": 101},
            ],
            "paging": {"page": 1, "pages": 1, "limit": 20},
            "connects": {"active": 4},
        },
        project={"success": True, "response": None},
        offer={"success": True, "response": None},
        offers={
            "success": True,
            "response": {"offers": [{"id": 8}]},
            "paging": {"page": 1, "pages": 1},
        },
        worker_orders={
            "success": True,
            "response": {"orders": "bad", "paging": {}},
        },
        get_order_details={"success": True, "response": None},
        get_kwork_details_extra={"success": True, "response": 3},
        kworks_status_list={
            "success": True,
            "response": [
                {"id": 1, "name": "empty", "kworks_count": 0, "kworks": []},
                {
                    "id": 2,
                    "name": "outer",
                    "kworks_count": 1,
                    "kworks": [{"id": 9, "title": "flat", "status_id": 2}],
                },
            ],
        },
        user_kworks={
            "success": True,
            "response": [{"id": 9, "title": "flat", "status_id": 2}],
            "paging": {"page": 1, "limit": 20, "total": 1, "pages": 1},
        },
    )
    gateway = gateway_for(config, client)

    with pytest.raises(GatewayError):
        await gateway.get_user(user_id=None, username="missing-id")
    with pytest.raises(GatewayError):
        await gateway.search_users("missing-id", 1)

    discovery = await gateway.discover_projects(
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
    assert discovery.projects.page is not None
    assert discovery.projects.page.high_watermark == "101:2"
    assert discovery.connects == {"active": 4}

    assert await gateway.get_project(1) is None
    assert await gateway.get_offer(1) is None
    client.responses["offer"] = {
        "success": True,
        "response": {"id": 8, "project_id": 10},
    }
    offers = await gateway.list_my_offers()
    assert offers.items[0].project_id == 10
    client.responses["offer"] = {"success": True, "response": None}
    with pytest.raises(GatewayError):
        await gateway.list_my_offers()

    with pytest.raises(GatewayError):
        await gateway.list_worker_orders()

    valid_kworks_response = client.responses["kworks_status_list"]
    client.responses["kworks_status_list"] = {
        "success": True,
        "response": [{"id": 1, "name": "missing-kworks", "kworks_count": 0}],
    }
    with pytest.raises(GatewayError) as missing_kworks:
        await gateway.list_my_kworks()
    assert missing_kworks.value.code is ErrorCode.CONTRACT_DRIFT
    assert missing_kworks.value.diagnostic == "kwork.group.items_not_array"
    assert await gateway.get_order_details(1) is None
    with pytest.raises(GatewayError):
        await gateway.get_kwork_details(1)

    client.responses["kworks_status_list"] = valid_kworks_response
    kworks = await gateway.list_my_kworks()
    assert [item.kwork_id for item in kworks.items] == [9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("embedded", "page_response", "diagnostic"),
    [
        (
            [{"id": 9, "status_id": 2}],
            [{"id": 10, "status_id": 2}],
            "userKworks:first_page_mismatch",
        ),
        (
            [{"id": 9, "status_id": 2}],
            [{"id": 9, "status_id": 3}],
            "kwork.item_status_group_mismatch",
        ),
    ],
)
async def test_kwork_status_embedded_page_and_full_page_must_agree(
    config_factory: Callable[..., KworkConfig],
    embedded: list[dict[str, Any]],
    page_response: list[dict[str, Any]],
    diagnostic: str,
) -> None:
    client = ScriptClient(
        kworks_status_list={
            "success": True,
            "response": [
                {
                    "id": 2,
                    "name": "Активные",
                    "kworks_count": 1,
                    "kworks": embedded,
                }
            ],
        },
        user_kworks={
            "success": True,
            "response": page_response,
            "paging": {"page": 1, "limit": 20, "total": 1, "pages": 1},
        },
    )

    with pytest.raises(GatewayError) as caught:
        await gateway_for(config_factory(), client).list_my_kworks()
    assert caught.value.code is ErrorCode.CONTRACT_DRIFT
    assert caught.value.diagnostic == diagnostic


@pytest.mark.asyncio
async def test_kwork_full_pagination_rejects_duplicate_stable_ids_across_groups(
    config_factory: Callable[..., KworkConfig],
) -> None:
    def user_kworks(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        status_id = kwargs["status_id"]
        return {
            "success": True,
            "response": [{"id": 9, "status_id": status_id}],
            "paging": {"page": 1, "limit": 20, "total": 1, "pages": 1},
        }

    client = ScriptClient(
        kworks_status_list={
            "success": True,
            "response": [
                {
                    "id": status_id,
                    "name": f"Группа {status_id}",
                    "kworks_count": 1,
                    "kworks": [{"id": 9, "status_id": status_id}],
                }
                for status_id in (1, 2)
            ],
        },
        user_kworks=user_kworks,
    )

    with pytest.raises(GatewayError) as caught:
        await gateway_for(config_factory(), client).list_my_kworks()
    assert caught.value.code is ErrorCode.CONTRACT_DRIFT
    assert caught.value.diagnostic == "userKworks:duplicate_kwork_id"


@pytest.mark.asyncio
async def test_read_detail_reraises_non_not_found_errors_and_scalar_responses(
    config_factory: Callable[..., KworkConfig],
) -> None:
    upstream_error = GatewayError(ErrorCode.PERMISSION)
    client = ScriptClient(
        project=upstream_error,
        offer=upstream_error,
        get_order_details=upstream_error,
        get_kwork_details_extra=upstream_error,
    )
    gateway = gateway_for(config_factory(), client)
    for operation in (
        lambda: gateway.get_project(1),
        lambda: gateway.get_offer(1),
        lambda: gateway.get_order_details(1),
        lambda: gateway.get_kwork_details(1),
    ):
        with pytest.raises(GatewayError) as caught:
            await operation()
        assert caught.value.code is ErrorCode.PERMISSION

    client.responses.update(
        project={"success": True, "response": 1},
        offer={"success": True, "response": 1},
        get_order_details={"success": True, "response": 1},
        get_kwork_details_extra={"success": True, "response": 1},
    )
    for operation in (
        lambda: gateway.get_project(1),
        lambda: gateway.get_offer(1),
        lambda: gateway.get_order_details(1),
        lambda: gateway.get_kwork_details(1),
    ):
        with pytest.raises(GatewayError):
            await operation()


class MatrixGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
    ) -> None:
        super().__init__(config, coordinator, DirectSession(object()))  # type: ignore[arg-type]
        self.project: ProjectRecord | None = ProjectRecord(
            project_id=10,
            status="active",
            raw={"id": 10},
        )
        self.offers: list[OfferRecord] = []
        self.connects = 3
        self.offer: OfferRecord | None = OfferRecord(
            offer_id=11,
            project_id=10,
            raw={"id": 11, "project_id": 10},
        )
        self.user: UserRecord | None = UserRecord(
            user_id=12,
            username="recipient",
            raw={"id": 12},
        )
        self.messages: list[MessageRecord] = [
            MessageRecord(
                message_id=13,
                sender_id=12,
                text="old",
                created_at=int(time.time()),
                raw={"id": 13},
            )
        ]
        self.dialogs: list[DialogRecord] = [DialogRecord(user_id=14, username="dialog", unread_count=2, raw={"id": 14})]
        self.orders: list[OrderRecord] = [OrderRecord(order_id=15, status=1, raw={"id": 15})]
        self.kworks: list[KworkRecord] = [
            KworkRecord(
                kwork_id=16,
                status_group_name="На проверке",
                raw={"id": 16},
            )
        ]

    async def get_project(self, project_id: int) -> ProjectRecord | None:
        return self.project

    async def _all_offers(self, max_pages: int = 50) -> list[OfferRecord]:
        return self.offers

    async def get_connects(self) -> ConnectsData:
        return ConnectsData(active=self.connects, total=self.connects, raw={})

    async def get_offer(self, offer_id: int) -> OfferRecord | None:
        return self.offer

    async def get_user(
        self,
        *,
        user_id: int | None,
        username: str | None,
    ) -> UserRecord | None:
        return self.user

    async def get_dialog(
        self,
        username: str,
        page: int = 1,
    ) -> ItemCollection[MessageRecord]:
        return ItemCollection[MessageRecord](
            items=self.messages,
            page=PageInfo(page=page, has_more=False),
        )

    async def list_dialogs(self, page: int = 1) -> ItemCollection[DialogRecord]:
        return ItemCollection[DialogRecord](
            items=self.dialogs,
            page=PageInfo(page=page, has_more=False),
        )

    async def _all_orders(self, max_pages: int = 50) -> list[OrderRecord]:
        return self.orders

    async def list_my_kworks(self) -> ItemCollection[KworkRecord]:
        return ItemCollection[KworkRecord](items=self.kworks)


def write_requests() -> list[Any]:
    return [
        SubmitOfferRequest(
            action=WriteAction.SUBMIT_OFFER,
            project_id=10,
            title="Offer",
            description="D" * 150,
            price=1000,
            duration_days=3,
        ),
        DeleteOfferRequest(action=WriteAction.DELETE_OFFER, offer_id=11),
        SendMessageRequest(
            action=WriteAction.SEND_MESSAGE,
            user_id=12,
            text="hello",
        ),
        SendMessageRequest(
            action=WriteAction.SEND_MESSAGE,
            username="recipient",
            text="hello",
        ),
        EditMessageRequest(
            action=WriteAction.EDIT_MESSAGE,
            message_id=13,
            username="recipient",
            text="edited",
        ),
        DeleteMessageRequest(
            action=WriteAction.DELETE_MESSAGE,
            message_id=13,
            username="recipient",
        ),
        MarkDialogReadRequest(action=WriteAction.MARK_DIALOG_READ, user_id=14),
        SubmitOrderApprovalRequest(
            action=WriteAction.SUBMIT_ORDER_APPROVAL,
            order_id=15,
        ),
        SetKworkStateRequest(
            action=WriteAction.SET_KWORK_STATE,
            kwork_id=16,
            target_state="active",
        ),
    ]


@pytest.mark.asyncio
async def test_all_prepare_preflight_success_branches(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    gateway = MatrixGateway(config, CoordinationStore(config))
    for request in write_requests():
        resolved: dict[str, Any] = {}
        await gateway._preflight(request, resolved)
        if request.action is WriteAction.SUBMIT_OFFER:
            assert resolved == {
                "project_status_at_prepare": "active",
                "connects_at_prepare": 3,
            }
        elif request.action is WriteAction.SEND_MESSAGE:
            assert resolved["user_id"] == 12
        elif request.action in {
            WriteAction.EDIT_MESSAGE,
            WriteAction.DELETE_MESSAGE,
        }:
            assert resolved["message_sender_id_at_prepare"] == 12
        elif request.action is WriteAction.MARK_DIALOG_READ:
            assert resolved["dialog_username_at_prepare"] == "dialog"
        elif request.action is WriteAction.SUBMIT_ORDER_APPROVAL:
            assert resolved["order_status_at_prepare"] == 1


@pytest.mark.asyncio
async def test_prepare_preflight_known_failure_branches(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    gateway = MatrixGateway(config, CoordinationStore(config))
    submit = write_requests()[0]

    gateway.project = None
    with pytest.raises(GatewayError) as missing_project:
        await gateway._preflight(submit, {})
    assert missing_project.value.code is ErrorCode.NOT_FOUND
    gateway.project = ProjectRecord(project_id=10, status="closed", raw={"id": 10})
    with pytest.raises(GatewayError) as closed:
        await gateway._preflight(submit, {})
    assert closed.value.code is ErrorCode.CLOSED_PROJECT
    gateway.project = ProjectRecord(project_id=10, status="active", raw={"id": 10})
    gateway.offers = [OfferRecord(offer_id=1, project_id=10, raw={"id": 1})]
    with pytest.raises(GatewayError) as duplicate:
        await gateway._preflight(submit, {})
    assert duplicate.value.code is ErrorCode.DUPLICATE
    gateway.offers = []
    gateway.connects = 0
    with pytest.raises(GatewayError) as no_connects:
        await gateway._preflight(submit, {})
    assert no_connects.value.code is ErrorCode.INSUFFICIENT_CONNECTS

    gateway.offer = None
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[1], {})
    gateway.user = None
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[3], {})
    gateway.messages = []
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[4], {})
    gateway.dialogs = []
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[6], {})
    gateway.orders = []
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[7], {})
    gateway.orders = [OrderRecord(order_id=15, status=2, raw={"id": 15})]
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[7], {})
    gateway.kworks = []
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[8], {})
    gateway.kworks = [KworkRecord(kwork_id=16, status_group_name="Активные", raw={"id": 16})]
    with pytest.raises(GatewayError):
        await gateway._preflight(write_requests()[8], {})


def stored_record(action: WriteAction) -> StoredWrite:
    now = time.time()
    return StoredWrite(
        write_id="id",
        scope="account-42",
        idempotency_key="key",
        action=action,
        payload_json="{}",
        payload_hash="a" * 64,
        state=WriteState.SUBMISSION_UNKNOWN,
        prepared_at=now - 1,
        expires_at=now + 100,
        updated_at=now,
        lease_owner=None,
        lease_expires=None,
        remote_started_at=None,
        result_json=None,
        error_json=None,
    )


@pytest.mark.asyncio
async def test_all_reconciliation_readback_action_branches(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(expected_user_id=42)
    gateway = MatrixGateway(config, CoordinationStore(config))
    request_by_action: list[tuple[WriteAction, dict[str, Any], dict[str, Any]]] = [
        (
            WriteAction.SUBMIT_OFFER,
            {
                "project_id": 10,
                "title": "Offer",
                "description": "D" * 150,
                "price": 1000,
                "duration_days": 3,
            },
            {},
        ),
        (WriteAction.DELETE_OFFER, {"offer_id": 11}, {}),
        (
            WriteAction.SEND_MESSAGE,
            {"username": "recipient", "text": "hello"},
            {"user_id": 12},
        ),
        (
            WriteAction.EDIT_MESSAGE,
            {"username": "recipient", "message_id": 13, "text": "hello"},
            {},
        ),
        (
            WriteAction.DELETE_MESSAGE,
            {"username": "recipient", "message_id": 999},
            {},
        ),
        (WriteAction.MARK_DIALOG_READ, {"user_id": 14}, {}),
        (WriteAction.SUBMIT_ORDER_APPROVAL, {"order_id": 15}, {}),
        (
            WriteAction.SET_KWORK_STATE,
            {"kwork_id": 16, "target_state": "active"},
            {},
        ),
    ]
    gateway.offers = [
        OfferRecord(
            offer_id=1,
            project_id=10,
            title="Offer",
            description="D" * 150,
            price=1000,
            duration_days=3,
            created_at=int(time.time()),
            raw={"id": 1},
        )
    ]
    gateway.messages = [
        MessageRecord(
            message_id=13,
            sender_id=42,
            text="hello",
            created_at=int(time.time()),
            raw={"id": 13},
        )
    ]
    gateway.dialogs = [DialogRecord(user_id=14, username="dialog", unread_count=0, raw={"id": 14})]
    gateway.orders = [OrderRecord(order_id=15, status=4, raw={"id": 15})]
    gateway.kworks = [KworkRecord(kwork_id=16, status_group_name="Активные", raw={"id": 16})]
    for action, request, resolved in request_by_action:
        if action is WriteAction.DELETE_OFFER:
            gateway.offer = None
        succeeded, result = await gateway._read_back(
            action,
            request,
            resolved,
            stored_record(action),
        )
        assert succeeded is True, (action, result)


class EmptyCookieJar:
    def filter_cookies(self, url: Any) -> dict[str, Any]:
        return {}


_FAKE_CSRF_HTML = "".join(
    (
        'csrf_user_token="',
        "abcdef0123456789",
        '" draftKey="draft123"',
    )
)


class FakeOfferWeb:
    base_url = "https://kwork.ru/"

    def __init__(
        self,
        *,
        page_status: int = 200,
        html: str = _FAKE_CSRF_HTML,
        final: dict[str, Any] | BaseException | None = None,
    ) -> None:
        self.page_status = page_status
        self.html = html
        self.final = final or {"status": 200, "json": {"success": True, "id": 901}}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def open_new_offer_page(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("page", params))
        return {"status": self.page_status, "text": self.html}

    async def quick_faq_init(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("faq", params))
        return {"status": 200}

    async def create_offer_draft(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("draft", params))
        return {"status": 200}

    async def check_is_template(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("template", params))
        return {"status": 200}

    async def create_exchange_offer(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("final", params))
        if isinstance(self.final, BaseException):
            raise self.final
        return self.final


class FakeOfferClient:
    def __init__(self, web: FakeOfferWeb) -> None:
        self.web = web
        self.session = SimpleNamespace(cookie_jar=EmptyCookieJar())


class OfferWebSession:
    scope = "account-42"

    def __init__(self, client: FakeOfferClient) -> None:
        self.client = client

    async def ensure_web_client(self) -> FakeOfferClient:
        return self.client

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


def submit_payload() -> dict[str, Any]:
    return {
        "project_id": 10,
        "title": "Offer",
        "description": "D" * 150,
        "price": 1000,
        "duration_days": 3,
    }


@pytest.mark.asyncio
async def test_real_submit_offer_web_flow_and_extractors(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    web = FakeOfferWeb()
    client = FakeOfferClient(web)
    gateway = KworkGateway(
        config,
        CoordinationStore(config),
        OfferWebSession(client),  # type: ignore[arg-type]
    )
    result = await gateway._execute_submit_offer(submit_payload())
    assert result == {"offer_id": 901, "project_id": 10}
    assert [name for name, _params in web.calls] == [
        "page",
        "faq",
        "draft",
        "template",
        "final",
    ]
    assert gateway._extract_offer_id({"offer_id": "5"}) == 5
    assert gateway._extract_offer_id({"response": {"id": 6}}) == 6
    assert gateway._extract_offer_id({"response": [{"offer_id": 7}]}) == 7
    assert gateway._extract_offer_id({"response": []}) is None
    assert gateway._draft_key('data-draft-key="custom99"') == "custom99"
    assert len(gateway._draft_key("")) == 8
    assert (
        gateway._extract_csrf(
            'name="csrftoken" value="abcdef0123456789"',
            client,  # type: ignore[arg-type]
        )
        == "abcdef0123456789"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("web", "expected_code"),
    [
        (FakeOfferWeb(page_status=500), ErrorCode.CSRF),
        (FakeOfferWeb(html="no csrf"), ErrorCode.CSRF),
        (
            FakeOfferWeb(
                final=GatewayError(
                    ErrorCode.TIMEOUT,
                    retryable=True,
                    safe_to_retry=True,
                )
            ),
            ErrorCode.AMBIGUOUS_WRITE,
        ),
        (
            FakeOfferWeb(final={"status": 500, "json": {}}),
            ErrorCode.AMBIGUOUS_WRITE,
        ),
        (
            FakeOfferWeb(final={"status": 200, "json": None}),
            ErrorCode.AMBIGUOUS_WRITE,
        ),
        (
            FakeOfferWeb(
                final={
                    "status": 200,
                    "json": {"success": False, "message": "duplicate"},
                }
            ),
            ErrorCode.DUPLICATE,
        ),
    ],
)
async def test_real_submit_offer_known_and_ambiguous_failures(
    config_factory: Callable[..., KworkConfig],
    web: FakeOfferWeb,
    expected_code: ErrorCode,
) -> None:
    config = config_factory()
    client = FakeOfferClient(web)
    gateway = KworkGateway(
        config,
        CoordinationStore(config),
        OfferWebSession(client),  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError) as caught:
        await gateway._execute_submit_offer(submit_payload())
    assert caught.value.code is expected_code


@pytest.mark.asyncio
async def test_submit_offer_without_id_requires_exact_readback(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    web = FakeOfferWeb(final={"status": 200, "json": {"success": True}})
    client = FakeOfferClient(web)
    gateway = MatrixGateway(config, CoordinationStore(config))
    gateway.session = OfferWebSession(client)  # type: ignore[assignment]
    gateway.offers = [
        OfferRecord(
            offer_id=902,
            project_id=10,
            title="Offer",
            description="D" * 150,
            price=1000,
            duration_days=3,
            created_at=int(time.time()),
            raw={"id": 902},
        )
    ]
    result = await gateway._execute_submit_offer(submit_payload())
    assert result["offer_id"] == 902
    gateway.offers = []
    with pytest.raises(GatewayError) as caught:
        await gateway._execute_submit_offer(submit_payload())
    assert caught.value.code is ErrorCode.AMBIGUOUS_WRITE


class CommitSession:
    scope = "account-42"

    async def verify_write_identity(self) -> Actor:
        return Actor(id=42, username="fixture")

    @asynccontextmanager
    async def exclusive_client(self) -> Any:
        yield


class CommitGateway(KworkGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        *,
        preflight_error: GatewayError | None = None,
        remote_outcome: dict[str, Any] | BaseException | None = None,
        resolved_user_id: int | None = None,
    ) -> None:
        super().__init__(config, coordinator, CommitSession())  # type: ignore[arg-type]
        self.preflight_error = preflight_error
        self.remote_outcome = remote_outcome or {"ok": True}
        self.resolved_user_id = resolved_user_id

    async def _preflight(self, request: Any, resolved: dict[str, Any]) -> None:
        if self.preflight_error is not None:
            raise self.preflight_error
        if self.resolved_user_id is not None:
            resolved["user_id"] = self.resolved_user_id

    async def _execute_write(
        self,
        record: StoredWrite,
        *,
        before_remote_attempt: Callable[[], Awaitable[None]],
    ) -> dict[str, Any]:
        await before_remote_attempt()
        if isinstance(self.remote_outcome, BaseException):
            raise self.remote_outcome
        return self.remote_outcome


async def prepare_commit_record(
    store: CoordinationStore,
    *,
    key: str,
    request: dict[str, Any],
    resolved: dict[str, Any] | None = None,
    prepared_account_id: Any = 42,
) -> tuple[StoredWrite, str]:
    prepared = await store.prepare_write(
        scope="account-42",
        idempotency_key=key,
        action=WriteAction(request["action"]),
        payload={
            "request": request,
            "resolved": resolved or {},
            "prepared_account_id": prepared_account_id,
        },
    )
    assert prepared.confirmation_token
    return prepared.record, prepared.confirmation_token


@pytest.mark.asyncio
async def test_commit_missing_and_known_preflight_failure_paths(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = CoordinationStore(config)
    gateway = CommitGateway(config, store)
    assert await gateway.get_write_status("missing", correlation_id="status") is None
    with pytest.raises(GatewayError) as missing:
        await gateway.commit_write(
            write_id="missing",
            payload_hash="a" * 64,
            confirmation_token="token",
            correlation_id="commit",
        )
    assert missing.value.code is ErrorCode.NOT_FOUND

    record, token = await prepare_commit_record(
        store,
        key="preflight-known",
        request={"action": "mark_dialog_read", "user_id": 1},
    )
    gateway.preflight_error = GatewayError(ErrorCode.NOT_FOUND)
    result = await gateway.commit_write(
        write_id=record.write_id,
        payload_hash=record.payload_hash,
        confirmation_token=token,
        correlation_id="commit",
    )
    assert result.state is WriteState.FAILED_KNOWN
    assert result.terminal_error is not None
    assert result.terminal_error.code is ErrorCode.NOT_FOUND

    record2, token2 = await prepare_commit_record(
        store,
        key="preflight-retryable",
        request={"action": "mark_dialog_read", "user_id": 1},
    )
    gateway.preflight_error = GatewayError(
        ErrorCode.TIMEOUT,
        retryable=True,
        safe_to_retry=True,
    )
    with pytest.raises(GatewayError) as retryable:
        await gateway.commit_write(
            write_id=record2.write_id,
            payload_hash=record2.payload_hash,
            confirmation_token=token2,
            correlation_id="commit",
        )
    assert retryable.value.code is ErrorCode.TIMEOUT
    persisted = await store.get_write(record2.write_id, scope="account-42")
    assert persisted is not None and persisted.state is WriteState.PREPARED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_error", "expected_state"),
    [
        (AmbiguousWriteError("ambiguous"), WriteState.SUBMISSION_UNKNOWN),
        (GatewayError(ErrorCode.PERMISSION), WriteState.FAILED_KNOWN),
        (GatewayError(ErrorCode.CONTRACT_DRIFT), WriteState.SUBMISSION_UNKNOWN),
        (RuntimeError("unexpected"), WriteState.SUBMISSION_UNKNOWN),
    ],
)
async def test_commit_remote_failure_classification_paths(
    config_factory: Callable[..., KworkConfig],
    remote_error: BaseException,
    expected_state: WriteState,
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)
    store = CoordinationStore(config)
    record, token = await prepare_commit_record(
        store,
        key=f"remote-{type(remote_error).__name__}-{expected_state.value}",
        request={"action": "mark_dialog_read", "user_id": 1},
    )
    gateway = CommitGateway(config, store, remote_outcome=remote_error)
    result = await gateway.commit_write(
        write_id=record.write_id,
        payload_hash=record.payload_hash,
        confirmation_token=token,
        correlation_id="commit",
    )
    assert result.state is expected_state


@pytest.mark.asyncio
async def test_commit_stored_payload_and_recipient_drift_fail_closed(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(enable_writes=True, expected_user_id=42)

    store = CoordinationStore(config)
    malformed, malformed_token = await prepare_commit_record(
        store,
        key="invalid-request",
        request={"action": "mark_dialog_read", "user_id": "not-an-int"},
    )
    malformed_result = await CommitGateway(config, store).commit_write(
        write_id=malformed.write_id,
        payload_hash=malformed.payload_hash,
        confirmation_token=malformed_token,
        correlation_id="commit",
    )
    assert malformed_result.state is WriteState.FAILED_KNOWN
    assert malformed_result.terminal_error is not None
    assert malformed_result.terminal_error.code is ErrorCode.CONTRACT_DRIFT

    missing_account, missing_token = await prepare_commit_record(
        store,
        key="missing-account",
        request={"action": "mark_dialog_read", "user_id": 1},
        prepared_account_id=None,
    )
    missing_result = await CommitGateway(config, store).commit_write(
        write_id=missing_account.write_id,
        payload_hash=missing_account.payload_hash,
        confirmation_token=missing_token,
        correlation_id="commit",
    )
    assert missing_result.state is WriteState.FAILED_KNOWN

    recipient, recipient_token = await prepare_commit_record(
        store,
        key="recipient-drift",
        request={
            "action": "send_message",
            "user_id": 12,
            "username": None,
            "text": "hello",
        },
        resolved={"user_id": 12},
    )
    recipient_result = await CommitGateway(
        config,
        store,
        resolved_user_id=13,
    ).commit_write(
        write_id=recipient.write_id,
        payload_hash=recipient.payload_hash,
        confirmation_token=recipient_token,
        correlation_id="commit",
    )
    assert recipient_result.state is WriteState.FAILED_KNOWN


class PagingGateway(MatrixGateway):
    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
    ) -> None:
        super().__init__(config, coordinator)
        self.always_more = False

    async def list_my_offers(self, page: int = 1) -> ItemCollection[OfferRecord]:
        return ItemCollection[OfferRecord](
            items=[OfferRecord(offer_id=page, project_id=10, raw={"id": page})],
            page=PageInfo(page=page, has_more=self.always_more or page == 1),
        )

    async def list_worker_orders(self, page: int = 1) -> ItemCollection[OrderRecord]:
        return ItemCollection[OrderRecord](
            items=[OrderRecord(order_id=page, raw={"id": page})],
            page=PageInfo(page=page, has_more=self.always_more or page == 1),
        )

    async def get_dialog(
        self,
        username: str,
        page: int = 1,
    ) -> ItemCollection[MessageRecord]:
        items = [MessageRecord(message_id=20, text="found", raw={"id": 20})] if page == 2 else []
        return ItemCollection[MessageRecord](
            items=items,
            page=PageInfo(page=page, has_more=self.always_more or page == 1),
        )

    async def list_dialogs(self, page: int = 1) -> ItemCollection[DialogRecord]:
        items = [DialogRecord(user_id=21, username="found", raw={"id": 21})] if page == 2 else []
        return ItemCollection[DialogRecord](
            items=items,
            page=PageInfo(page=page, has_more=self.always_more or page == 1),
        )


@pytest.mark.asyncio
async def test_pagination_aggregation_find_and_safety_limits(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()
    gateway = PagingGateway(config, CoordinationStore(config))
    assert [item.offer_id for item in await KworkGateway._all_offers(gateway)] == [1, 2]
    assert [item.order_id for item in await KworkGateway._all_orders(gateway)] == [1, 2]
    assert (await gateway._find_message(username="user", message_id=20)).message_id == 20  # type: ignore[union-attr]
    assert (await gateway._find_dialog_by_user_id(21)).username == "found"  # type: ignore[union-attr]
    assert await gateway._find_message(username="user", message_id=999) is None
    assert await gateway._find_dialog_by_user_id(999) is None

    gateway.always_more = True
    with pytest.raises(GatewayError):
        await KworkGateway._all_offers(gateway, max_pages=1)
    with pytest.raises(GatewayError):
        await KworkGateway._all_orders(gateway, max_pages=1)
    with pytest.raises(GatewayError):
        await gateway._find_message(
            username="user",
            message_id=999,
            max_pages=1,
        )
    with pytest.raises(GatewayError):
        await gateway._find_dialog_by_user_id(999, max_pages=1)


@pytest.mark.asyncio
async def test_message_and_offer_matching_edge_states(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(expected_user_id=42)
    gateway = MatrixGateway(config, CoordinationStore(config))
    now = int(time.time())
    request = submit_payload()
    assert (
        gateway._offer_fingerprint_state(
            OfferRecord(
                offer_id=1,
                project_id=10,
                title="different",
                description=request["description"],
                price=1000,
                duration_days=3,
                raw={"id": 1},
            ),
            request,
        )
        == "different"
    )
    assert (
        gateway._offer_fingerprint_state(
            OfferRecord(offer_id=1, project_id=10, raw={"id": 1}),
            request,
        )
        == "inconclusive"
    )
    assert (
        gateway._offer_fingerprint_state(
            OfferRecord(
                offer_id=1,
                project_id=10,
                title="Offer",
                description=request["description"],
                price=1000,
                duration_days=3,
                created_at=None,
                raw={"id": 1},
            ),
            request,
            prepared_at=now,
            unknown_at=now,
        )
        == "inconclusive"
    )
    assert (
        gateway._offer_fingerprint_state(
            OfferRecord(
                offer_id=1,
                project_id=10,
                title="Offer",
                description=request["description"],
                price=1000,
                duration_days=3,
                created_at=1,
                raw={"id": 1},
            ),
            request,
            prepared_at=now,
            unknown_at=now,
        )
        == "different"
    )

    gateway.offers = [
        OfferRecord(offer_id=1, project_id=99, raw={"id": 1}),
        OfferRecord(offer_id=2, project_id=10, raw={"id": 2}),
    ]
    gateway.offer = None
    with pytest.raises(GatewayError) as inconclusive:
        await gateway._matching_offers(request)
    assert inconclusive.value.code is ErrorCode.AMBIGUOUS_WRITE

    gateway.messages = [
        MessageRecord(message_id=1, sender_id=42, text="other", created_at=now, raw={}),
        MessageRecord(message_id=2, sender_id=None, text="target", created_at=now, raw={}),
        MessageRecord(message_id=3, sender_id=99, text="target", created_at=now, raw={}),
        MessageRecord(message_id=4, sender_id=42, text="target", created_at=now, raw={}),
    ]
    matches = await gateway._matching_sent_messages(
        username="recipient",
        text="target",
        sender_id=42,
        prepared_at=now,
        unknown_at=now,
    )
    assert [message.message_id for message in matches] == [4]


@pytest.mark.asyncio
async def test_reconciliation_negative_and_ambiguous_branches(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory(expected_user_id=42)
    gateway = MatrixGateway(config, CoordinationStore(config))
    now = int(time.time())
    gateway.offers = [
        OfferRecord(
            offer_id=value,
            project_id=10,
            title="Offer",
            description="D" * 150,
            price=1000,
            duration_days=3,
            created_at=now,
            raw={"id": value},
        )
        for value in (1, 2)
    ]
    with pytest.raises(GatewayError) as multiple_offers:
        await gateway._read_back(
            WriteAction.SUBMIT_OFFER,
            submit_payload(),
            {},
            stored_record(WriteAction.SUBMIT_OFFER),
        )
    assert multiple_offers.value.code is ErrorCode.AMBIGUOUS_WRITE

    with pytest.raises(GatewayError):
        await gateway._read_back(
            WriteAction.SEND_MESSAGE,
            {"username": "recipient", "text": "hello"},
            {},
            stored_record(WriteAction.SEND_MESSAGE),
        )
    gateway.messages = [
        MessageRecord(
            message_id=value,
            sender_id=42,
            text="hello",
            created_at=now,
            raw={"id": value},
        )
        for value in (1, 2)
    ]
    with pytest.raises(GatewayError) as multiple_messages:
        await gateway._read_back(
            WriteAction.SEND_MESSAGE,
            {"username": "recipient", "text": "hello"},
            {"user_id": 12},
            stored_record(WriteAction.SEND_MESSAGE),
        )
    assert multiple_messages.value.code is ErrorCode.AMBIGUOUS_WRITE

    gateway.messages = []
    succeeded, _ = await gateway._read_back(
        WriteAction.EDIT_MESSAGE,
        {"username": "recipient", "message_id": 1, "text": "new"},
        {},
        stored_record(WriteAction.EDIT_MESSAGE),
    )
    assert succeeded is False
    gateway.dialogs = []
    assert (
        await gateway._read_back(
            WriteAction.MARK_DIALOG_READ,
            {"user_id": 14},
            {},
            stored_record(WriteAction.MARK_DIALOG_READ),
        )
    )[0] is False
    gateway.orders = []
    assert (
        await gateway._read_back(
            WriteAction.SUBMIT_ORDER_APPROVAL,
            {"order_id": 15},
            {},
            stored_record(WriteAction.SUBMIT_ORDER_APPROVAL),
        )
    )[0] is False
    gateway.kworks = [KworkRecord(kwork_id=16, status_group_name="На паузе", raw={"id": 16})]
    assert (
        await gateway._read_back(
            WriteAction.SET_KWORK_STATE,
            {"kwork_id": 16, "target_state": "paused"},
            {},
            stored_record(WriteAction.SET_KWORK_STATE),
        )
    )[0] is True
