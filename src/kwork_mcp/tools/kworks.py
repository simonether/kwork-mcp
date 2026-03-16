from __future__ import annotations

import json
from typing import Any

from fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import (
    ANNO_DESTRUCTIVE,
    ANNO_READ,
    ANNO_WRITE,
    check_success,
    extract_error,
    unwrap_response,
    validate_positive_id,
)

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
    response = unwrap_response(data)
    if not isinstance(response, dict):
        return json.dumps(data, ensure_ascii=False, indent=2)

    lines: list[str] = []

    good = response.get("goodReviews", 0)
    bad = response.get("badReviews", 0)
    reviews_count = response.get("reviews_count", 0)
    faq_count = response.get("frequently_asked_questions_count", 0)
    not_avail = response.get("not_available_for_company")

    lines.append(f"Отзывы: {reviews_count} (положительных: {good}, отрицательных: {bad})")
    if faq_count:
        lines.append(f"Часто задаваемых вопросов: {faq_count}")
    if not_avail is not None:
        lines.append(f"Недоступен для компаний: {'да' if not_avail else 'нет'}")

    similar = response.get("similar_kworks") or []
    if similar:
        lines.append(f"\nПохожие кворки ({len(similar)}):")
        for kw in similar[:5]:
            if not isinstance(kw, dict):
                continue
            title = kw.get("title", "—")
            price = kw.get("price", "?")
            worker = kw.get("worker", {})
            username = worker.get("username", "?") if isinstance(worker, dict) else "?"
            rating = worker.get("rating", "?") if isinstance(worker, dict) else "?"
            lines.append(f"  #{kw.get('id', '?')} | {title} | {price} руб. | @{username} ({rating})")
        if len(similar) > 5:
            lines.append(f"  ... и ещё {len(similar) - 5}")

    if not lines:
        return json.dumps(response, ensure_ascii=False, indent=2)

    lines.append("\nПримечание: название, цена и описание кворка доступны через list_my_kworks.")
    return "\n".join(lines)


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ANNO_READ)
    async def list_my_kworks(ctx: Context) -> str:
        """Получить список своих кворков (услуг), сгруппированных по статусу.

        Когда использовать: для просмотра всех своих услуг и их текущих статусов.
        Возвращает: кворки, сгруппированные по статусам (активные, на паузе и т.д.).
        Связанные: get_kwork_details — подробности кворка, start_kwork/pause_kwork — управление.
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_my_kworks", session=session):
            data = await client.kworks_status_list()

        response = unwrap_response(data)
        if not response:
            return "Кворков нет."

        lines = ["Мои кворки:"]
        found_any = False

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
                    if "kworks" in kwork or "kworks_count" in kwork:
                        continue
                    lines.append(_format_kwork_brief(kwork))

        if not found_any:
            return "Кворков нет."

        return "\n".join(lines)

    @mcp.tool(annotations=ANNO_READ)
    async def get_kwork_details(kwork_id: int, ctx: Context) -> str:
        """Получить подробную информацию о кворке по его ID.

        Когда использовать: для просмотра отзывов, FAQ и похожих кворков.
        Возвращает: отзывы, часто задаваемые вопросы, похожие кворки.
        Связанные: list_my_kworks — название и цена кворка.
        """
        validate_positive_id(kwork_id, "kwork_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("get_kwork_details", session=session):
            data = await client.get_kwork_details_extra(id=kwork_id, use_token=True)

        return _format_kwork_detail(data)

    @mcp.tool(annotations=ANNO_WRITE)
    async def start_kwork(kwork_id: int, ctx: Context) -> str:
        """Активировать приостановленный кворк. Кворк станет виден покупателям.

        Когда использовать: для возобновления показа кворка после паузы.
        Возвращает: подтверждение активации или сообщение об ошибке.
        Связанные: pause_kwork — приостановить кворк, list_my_kworks — список кворков.
        """
        validate_positive_id(kwork_id, "kwork_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("start_kwork", session=session):
            data = await client.start_kwork(kwork_id=kwork_id)

        if check_success(data):
            return f"Кворк #{kwork_id} активирован."

        return f"Не удалось активировать кворк #{kwork_id}: {extract_error(data)}"

    @mcp.tool(annotations=ANNO_DESTRUCTIVE)
    async def pause_kwork(kwork_id: int, ctx: Context) -> str:
        """Приостановить кворк. Кворк перестанет отображаться покупателям.

        Когда использовать: для временного скрытия кворка (отпуск, перегрузка).
        Возвращает: подтверждение приостановки или сообщение об ошибке.
        Связанные: start_kwork — возобновить кворк.
        """
        validate_positive_id(kwork_id, "kwork_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("pause_kwork", session=session):
            data = await client.pause_kwork(kwork_id=kwork_id)

        if check_success(data):
            return f"Кворк #{kwork_id} приостановлен."

        return f"Не удалось приостановить кворк #{kwork_id}: {extract_error(data)}"
