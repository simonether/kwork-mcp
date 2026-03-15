from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager

_KWORK_STATUS_NAMES: dict[str, str] = {
    "active": "Активные",
    "paused": "На паузе",
    "moderation": "На модерации",
    "denied": "Отклонённые",
    "draft": "Черновики",
}


def _format_kwork_brief(kwork: dict[str, Any]) -> str:
    kid = kwork.get("id", "—")
    name = kwork.get("title") or kwork.get("name") or "—"
    price = kwork.get("price") or "—"
    orders = kwork.get("orders_count") or kwork.get("order_count") or 0
    return f"    #{kid} | {name} | {price} руб. | заказов: {orders}"


def _format_kwork_detail(data: dict[str, Any]) -> str:
    response = data.get("response") or data
    # Response may wrap kwork under a "kwork" key or be the kwork itself
    if isinstance(response, dict) and "kwork" in response and isinstance(response["kwork"], dict):
        kwork = response["kwork"]
    elif isinstance(response, dict):
        kwork = response
    else:
        kwork = data

    kid = kwork.get("id", "—")
    name = kwork.get("title") or kwork.get("name") or kwork.get("lang") or "—"
    price = kwork.get("price") or "—"
    category = kwork.get("category_name") or kwork.get("category") or "—"
    raw_status = kwork.get("status") or kwork.get("status_name") or "—"
    status = _KWORK_STATUS_NAMES.get(str(raw_status), str(raw_status))
    orders = kwork.get("orders_count") or kwork.get("order_count") or 0
    reviews = kwork.get("reviews_count") or kwork.get("review_count") or 0
    description = kwork.get("description") or "—"
    days = kwork.get("days") or kwork.get("delivery_days") or "—"

    lines = [
        f"Кворк #{kid}",
        f"Название: {name}",
        f"Цена: {price} руб.",
        f"Категория: {category}",
        f"Статус: {status}",
        f"Срок выполнения: {days} дн.",
        f"Количество заказов: {orders}",
        f"Количество отзывов: {reviews}",
        f"Описание: {description}",
    ]

    # Fallback: if most fields are empty, include raw JSON for debugging
    if name == "—" and price == "—" and description == "—":
        lines.append(f"\nСырые данные:\n{json.dumps(response, ensure_ascii=False, indent=2)}")

    return "\n".join(lines)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_my_kworks(ctx: Context) -> str:
        """Получить список своих кворков (услуг), сгруппированных по статусу."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_my_kworks"):
            data = await client.kworks_status_list()

        response = data.get("response") or data
        if not response:
            return "Кворков нет."

        lines = ["Мои кворки:"]
        found_any = False

        # Response may be a dict with status keys or a list of status groups
        if isinstance(response, dict):
            for status_key, display_name in _KWORK_STATUS_NAMES.items():
                kworks = response.get(status_key) or response.get(f"{status_key}_kworks") or []
                if isinstance(kworks, dict):
                    kworks = kworks.get("data") or kworks.get("items") or []
                if not kworks:
                    continue
                found_any = True
                lines.append(f"\n  {display_name}:")
                if isinstance(kworks, list):
                    for kwork in kworks:
                        if isinstance(kwork, dict):
                            lines.append(_format_kwork_brief(kwork))
                        else:
                            lines.append(f"    {kwork}")
            # Handle unexpected keys
            if not found_any:
                for key, value in response.items():
                    if isinstance(value, list) and value:
                        found_any = True
                        label = _KWORK_STATUS_NAMES.get(key, key)
                        lines.append(f"\n  {label}:")
                        for kwork in value:
                            if isinstance(kwork, dict):
                                lines.append(_format_kwork_brief(kwork))
                            else:
                                lines.append(f"    {kwork}")
        elif isinstance(response, list):
            # pykwork returns list of status groups:
            # [{"id": 7, "name": "Активные", "kworks": [{...}, ...], "kworks_count": 1}, ...]
            for group in response:
                if not isinstance(group, dict):
                    continue
                group_kworks = group.get("kworks")
                if not isinstance(group_kworks, list) or not group_kworks:
                    continue
                found_any = True
                label = group.get("name") or str(group.get("id", "?"))
                lines.append(f"\n  {label}:")
                for kwork in group_kworks:
                    if not isinstance(kwork, dict):
                        lines.append(f"    {kwork}")
                        continue
                    # Skip nested status groups (they have "kworks" key)
                    if "kworks" in kwork or "kworks_count" in kwork:
                        continue
                    lines.append(_format_kwork_brief(kwork))

        if not found_any:
            return "Кворков нет."

        return "\n".join(lines)

    @mcp.tool()
    async def get_kwork_details(kwork_id: int, ctx: Context) -> str:
        """Получить подробную информацию о кворке по его ID."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        # getKworkDetails returns minimal data; getKworkDetailsExtra returns full info
        async with api_guard("get_kwork_details"):
            data = await client.get_kwork_details_extra(id=kwork_id, use_token=True)

        return _format_kwork_detail(data)

    @mcp.tool()
    async def start_kwork(kwork_id: int, ctx: Context) -> str:
        """Активировать приостановленный кворк. Кворк станет виден покупателям."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("start_kwork"):
            data = await client.start_kwork(kwork_id=kwork_id)

        if data.get("success"):
            return f"Кворк #{kwork_id} активирован."

        error = data.get("error") or data.get("message") or data
        return f"Не удалось активировать кворк #{kwork_id}: {error}"

    @mcp.tool()
    async def pause_kwork(kwork_id: int, ctx: Context) -> str:
        """Приостановить кворк. Кворк перестанет отображаться покупателям."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("pause_kwork"):
            data = await client.pause_kwork(kwork_id=kwork_id)

        if data.get("success"):
            return f"Кворк #{kwork_id} приостановлен."

        error = data.get("error") or data.get("message") or data
        return f"Не удалось приостановить кворк #{kwork_id}: {error}"
