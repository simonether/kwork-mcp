from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from kwork.schema.actor import Actor

from kwork_mcp.config import KworkConfig
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError
from kwork_mcp.gateway import KworkGateway
from kwork_mcp.models import ErrorCode
from kwork_mcp.session import KworkSessionManager


class RestartProjectClient:
    def __init__(self, *, user_id: int, username: str) -> None:
        self.actor = Actor(id=user_id, username=username)
        self._token: str | None = None
        self.get_me_calls = 0
        self.project_pages: list[int] = []
        self.closed = False

    async def get_me(self) -> Actor:
        self.get_me_calls += 1
        return self.actor

    async def projects(self, *, use_token: bool, **params: Any) -> dict[str, Any]:
        assert use_token is True
        page = int(params["page"])
        self.project_pages.append(page)
        return {
            "success": True,
            "response": [
                {
                    "id": 100 + page,
                    "title": f"Страница {page}",
                    "description": "Недоверенный внешний текст",
                    "date_confirm": 1000 + page,
                }
            ],
            "paging": {"page": page, "limit": 1, "total": 2, "pages": 2},
        }

    async def close(self) -> None:
        self.closed = True


async def discover_all(gateway: KworkGateway, *, cursor: str | None = None) -> Any:
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
async def test_persistent_discovery_cursor_rebinds_identity_after_restart(
    config_factory: Callable[..., KworkConfig],
) -> None:
    config = config_factory()

    first_client = RestartProjectClient(user_id=42, username="fixture")
    first_session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: first_client,  # type: ignore[arg-type]
    )
    first_gateway = KworkGateway(config, first_session.coordinator, first_session)

    assert first_session.scope == config.bootstrap_scope
    first_page = await discover_all(first_gateway)
    assert first_session.scope == "account-42"
    assert first_page.projects.page is not None
    cursor = first_page.projects.page.next_cursor
    assert cursor is not None
    cursor_payload = await first_gateway.cursor_codec.decode(cursor)
    assert cursor_payload["scope"] == "account-42"
    await first_session.close()

    restarted_client = RestartProjectClient(user_id=42, username="fixture")
    restarted_session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: restarted_client,  # type: ignore[arg-type]
    )
    restarted_gateway = KworkGateway(
        config,
        restarted_session.coordinator,
        restarted_session,
    )

    assert restarted_session.scope == config.bootstrap_scope
    second_page = await discover_all(restarted_gateway, cursor=cursor)
    assert restarted_session.scope == "account-42"
    assert restarted_client.get_me_calls == 1
    assert restarted_client.project_pages == [2]
    assert [project.project_id for project in second_page.projects.items] == [102]
    await restarted_session.close()

    other_client = RestartProjectClient(user_id=99, username="other")
    other_session = KworkSessionManager(
        config,
        CoordinationStore(config),
        client_factory=lambda _: other_client,  # type: ignore[arg-type]
    )
    other_gateway = KworkGateway(config, other_session.coordinator, other_session)

    assert other_session.scope == config.bootstrap_scope
    with pytest.raises(GatewayError) as cross_account:
        await discover_all(other_gateway, cursor=cursor)
    assert cross_account.value.code is ErrorCode.VALIDATION
    assert other_session.scope == "account-99"
    assert other_client.get_me_calls == 1
    assert other_client.project_pages == []
    await other_session.close()
