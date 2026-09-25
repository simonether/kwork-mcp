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
