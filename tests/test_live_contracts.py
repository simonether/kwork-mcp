"""Response shapes observed on a live account (values anonymized)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from kwork.exceptions import KworkHTTPException
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError, classify_upstream_error
from kwork_mcp.gateway import KworkGateway

ACCOUNT_ID = 4242


class LiveShapeSession:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.scope = f"account-{ACCOUNT_ID}"
        self.actor = Actor(id=ACCOUNT_ID, username="fixture")

    async def call_read(self, _route: str, operation: Callable[[Any], Awaitable[Any]]) -> Any:
        try:
            return await operation(self.client)
        except Exception as exc:
            raise classify_upstream_error(exc) from exc


def _gateway(config_factory: Callable[..., KworkConfig], client: Any) -> KworkGateway:
    config = config_factory()
    return KworkGateway(config, CoordinationStore(config), LiveShapeSession(client))  # type: ignore[arg-type]


def _kwork(kwork_id: int, status_id: int) -> dict[str, Any]:
    return {
        "id": kwork_id,
        "category_id": 41,
        "status_id": status_id,
        "status_name": "fixture",
        "title": f"Кворк {kwork_id}",
        "price": 1000,
        "worker": {"id": ACCOUNT_ID, "username": "fixture"},
        "activity": {"views": 0, "orders": 0, "earned": 0},
    }


# Status groups exactly as kworksStatusList returned them: the trailing
# aggregate "all kworks" group has id 0 and repeats every kwork (first page only).
_GROUPS: dict[int, tuple[str, list[int]]] = {
    7: ("Активные", [101]),
    1: ("На модерации", []),
    2: ("Требуют исправления", [102]),
    3: ("Остановленные", [103, 104]),
    4: ("На паузе", []),
    5: ("Скрытые", []),
    6: ("Черновики", [105]),
}


class KworksClient:
    def __init__(self) -> None:
        self.user_kworks_calls: list[dict[str, Any]] = []

    async def kworks_status_list(self, *, use_token: bool) -> dict[str, Any]:
        groups = [
            {
                "id": group_id,
                "name": name,
                "kworks_count": len(ids),
                "kworks": [_kwork(kwork_id, group_id) for kwork_id in ids],
            }
            for group_id, (name, ids) in _GROUPS.items()
        ]
        everything = [_kwork(kwork_id, group_id) for group_id, (_name, ids) in _GROUPS.items() for kwork_id in ids]
        groups.append({"id": 0, "name": "Все", "kworks_count": len(everything), "kworks": everything})
        return {"success": True, "response": groups}

    async def user_kworks(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        self.user_kworks_calls.append(params)
        ids = _GROUPS[params["status_id"]][1]
        return {
            "success": True,
            "response": [_kwork(kwork_id, params["status_id"]) for kwork_id in ids],
            "paging": {"page": 1, "total": len(ids), "limit": 10, "pages": 1},
        }


@pytest.mark.asyncio
async def test_list_my_kworks_skips_aggregate_all_group(
    config_factory: Callable[..., KworkConfig],
) -> None:
    client = KworksClient()
    result = await _gateway(config_factory, client).list_my_kworks()

    assert sorted(item.kwork_id for item in result.items) == [101, 102, 103, 104, 105]
    assert {item.kwork_id: item.status_group_id for item in result.items} == {
        101: 7,
        102: 2,
        103: 3,
        104: 3,
        105: 6,
    }
    assert all(call["status_id"] != 0 for call in client.user_kworks_calls)


class NestedGroupsClient(KworksClient):
    """Older shape: status groups nested inside another group's ``kworks``."""

    async def kworks_status_list(self, *, use_token: bool) -> dict[str, Any]:
        return {
            "success": True,
            "response": [
                {
                    "id": 7,
                    "name": "Активные",
                    "kworks_count": 1,
                    "kworks": [
                        _kwork(101, 7),
                        {"id": 1, "name": "На модерации", "kworks_count": 0, "kworks": []},
                        {"id": 2, "name": "Требуют исправления", "kworks_count": 1, "kworks": [_kwork(102, 2)]},
                    ],
                },
                {"id": 4, "name": "На паузе", "kworks_count": 0, "kworks": []},
            ],
        }


@pytest.mark.asyncio
async def test_list_my_kworks_visits_nested_status_groups(
    config_factory: Callable[..., KworkConfig],
) -> None:
    result = await _gateway(config_factory, NestedGroupsClient()).list_my_kworks()

    assert {item.kwork_id: item.status_group_name for item in result.items} == {
        101: "Активные",
        102: "Требуют исправления",
    }


class ExchangeInfoClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def exchange_info(self, *, use_token: bool) -> dict[str, Any]:
        raise KworkHTTPException(
            "Kwork API rejected /exchangeInfo",
            status=200,
            endpoint="exchangeInfo",
            response_json=self.payload,
        )


@pytest.mark.asyncio
async def test_exchange_info_accepts_bare_object_without_success_flag(
    config_factory: Callable[..., KworkConfig],
) -> None:
    payload = {"archived_count": 0, "exchange_response_count": 14}
    result = await _gateway(config_factory, ExchangeInfoClient(payload)).get_exchange_info()

    assert result.raw == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error": "Нет доступа"},
        {"error": "Нет доступа", "error_code": 403},
    ],
)
async def test_exchange_info_still_rejects_error_payloads(
    config_factory: Callable[..., KworkConfig],
    payload: dict[str, Any],
) -> None:
    with pytest.raises(GatewayError):
        await _gateway(config_factory, ExchangeInfoClient(payload)).get_exchange_info()


class EmptyNotificationsClient:
    async def get_notifications(self) -> dict[str, Any]:
        return {"success": True}


@pytest.mark.asyncio
async def test_notifications_without_response_key_are_empty(
    config_factory: Callable[..., KworkConfig],
) -> None:
    result = await _gateway(config_factory, EmptyNotificationsClient()).list_notifications()

    assert result.raw == []


def test_offer_readback_matches_the_text_kwork_stores() -> None:
    """Live 2026-10-02: Kwork stored a sent offer with blank lines collapsed and «» escaped."""
    from kwork_mcp.models import OfferRecord

    request = {
        "title": "Бот: этап 1, «сценарий»",
        "description": "Первая строка.\n\nВторая строка с «кавычками».\n\n\nТретья.",
        "price": 50000,
        "duration_days": 12,
    }
    stored = OfferRecord(
        offer_id=1,
        project_id=2,
        title="Бот: этап 1, &laquo;сценарий&raquo;",
        description="Первая строка.\nВторая строка с &laquo;кавычками&raquo;.\nТретья.",
        price=50000,
        duration_days=12,
        created_at=1_000,
        raw={},
    )

    assert KworkGateway._offer_fingerprint_state(stored, request) == "match"
    changed = stored.model_copy(update={"description": "Первая строка.\nДругой текст.\nТретья."})
    assert KworkGateway._offer_fingerprint_state(changed, request) == "different"


# A dialog of 101 messages as inboxes returned it on 2026-10-02: pages are
# numbered from the oldest message and filled from the newest end, so page 1
# holds the single oldest message. Without a page Kwork answers with the last one.
_DIALOG_TOTAL = 101
_DIALOG_LIMIT = 50


def _dialog_page(page: int) -> list[dict[str, Any]]:
    pages = -(-_DIALOG_TOTAL // _DIALOG_LIMIT)
    first = _DIALOG_TOTAL - (pages - 1) * _DIALOG_LIMIT
    start = 0 if page == 1 else first + (page - 2) * _DIALOG_LIMIT
    size = first if page == 1 else _DIALOG_LIMIT
    # Newest first within a page, like the live response.
    return [
        {"message_id": 1000 + index, "from_id": 7, "message": f"m{index}", "time": 10_000 + index}
        for index in reversed(range(start, start + size))
    ]


class LongDialogClient:
    def __init__(self, *, short_page: int = 1) -> None:
        self.short_page = short_page
        self.requested: list[int | None] = []

    async def inboxes(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        page = params.get("page")
        self.requested.append(page)
        pages = -(-_DIALOG_TOTAL // _DIALOG_LIMIT)
        current = pages if page is None else page
        items = _dialog_page(current)
        if self.short_page != 1 and current == 1:
            items = _dialog_page(2)  # a page 1 that is full breaks the live layout
        return {
            "success": True,
            "response": items,
            "paging": {"page": current, "pages": pages, "total": _DIALOG_TOTAL, "limit": _DIALOG_LIMIT},
        }


@pytest.mark.asyncio
async def test_long_dialog_pages_fill_from_the_newest_end(config_factory: Callable[..., KworkConfig]) -> None:
    client = LongDialogClient()
    gateway = _gateway(config_factory, client)

    latest = await gateway.get_dialog("fixture")
    oldest = await gateway.get_dialog("fixture", 1)
    middle = await gateway.get_dialog("fixture", 2)

    assert client.requested == [None, 1, 2]
    assert latest.page is not None and latest.page.page == 3 and latest.page.total_pages == 3
    assert len(latest.items) == 50 and latest.items[0].message_id == 1100
    assert [item.message_id for item in oldest.items] == [1000]
    assert len(middle.items) == 50


@pytest.mark.asyncio
async def test_long_dialog_with_a_full_first_page_is_drift(config_factory: Callable[..., KworkConfig]) -> None:
    gateway = _gateway(config_factory, LongDialogClient(short_page=0))

    with pytest.raises(GatewayError) as drift:
        await gateway.get_dialog("fixture", 1)

    assert drift.value.diagnostic == "inboxes:paging_item_count_inconsistent"


class StaleLatestPageClient:
    async def inboxes(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        return {
            "success": True,
            "response": _dialog_page(2),
            "paging": {"page": 2, "pages": 3, "total": _DIALOG_TOTAL, "limit": _DIALOG_LIMIT},
        }


@pytest.mark.asyncio
async def test_dialog_without_page_must_answer_with_the_last_page(config_factory: Callable[..., KworkConfig]) -> None:
    gateway = _gateway(config_factory, StaleLatestPageClient())

    with pytest.raises(GatewayError) as drift:
        await gateway.get_dialog("fixture")

    assert drift.value.diagnostic == "inboxes:paging_latest_page_mismatch"


def test_message_text_matches_the_html_escaped_form_kwork_returns() -> None:
    """Live 2026-10-02: a sent ASCII quote read back as &quot;, while «» and blank lines stayed."""
    from kwork_mcp.gateway.parsing import _normalize_message_text

    sent = 'Связка "Модель X" уже в индексе.\n\nДальше «по плану».'
    stored = "Связка &quot;Модель X&quot; уже в индексе.\n\nДальше «по плану»."

    assert _normalize_message_text(stored) == _normalize_message_text(sent)
    assert _normalize_message_text(stored) != _normalize_message_text(sent.replace("\n\n", "\n"))
