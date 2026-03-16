from __future__ import annotations

import json

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import (
    ANNO_DESTRUCTIVE,
    ANNO_READ,
    ANNO_WRITE,
    safe_get,
    unwrap_response,
    validate_not_empty,
    validate_positive_id,
    validate_positive_int,
)

_MIN_DESCRIPTION_LENGTH = 150


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ANNO_READ)
    async def list_my_offers(ctx: Context) -> str:
        """Список ваших предложений (офферов) на бирже Kwork.

        Когда использовать: для просмотра отправленных предложений и их статусов.
        Возвращает: список предложений с ID, названием проекта, статусом, ценой.
        Связанные: get_offer — подробности предложения, submit_offer — отправить новое.
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("список предложений", session=session):
            result = await client.offers(use_token=True)

        resp = unwrap_response(result)

        if isinstance(resp, list):
            items = resp
        elif isinstance(resp, dict):
            items = resp.get("offers") or resp.get("items") or []
        else:
            items = []

        if not items:
            return "У вас нет активных предложений на бирже."

        lines = [f"Ваши предложения: {len(items)}\n"]
        for offer in items:
            if not isinstance(offer, dict):
                continue
            oid = offer.get("id", "?")
            project_name = safe_get(offer, "name", "want_name", "title")
            status = offer.get("status") or "—"
            price = safe_get(offer, "price", "kwork_price")
            lines.append(f"[ID {oid}] {project_name}\n  Статус: {status} | Цена: {price} руб.")
        return "\n\n".join(lines)

    @mcp.tool(annotations=ANNO_READ)
    async def get_offer(offer_id: int, ctx: Context) -> str:
        """Подробная информация о вашем предложении на бирже Kwork.

        Когда использовать: для просмотра деталей конкретного предложения.
        Возвращает: название, статус, цена, срок, проект, описание.
        Связанные: list_my_offers — список всех предложений, delete_offer — удалить.
        """
        validate_positive_id(offer_id, "offer_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("получение предложения", session=session):
            result = await client.offer(id=offer_id, use_token=True)

        resp = unwrap_response(result)

        if isinstance(resp, dict):
            oid = resp.get("id", offer_id)
            title = safe_get(resp, "name", "kwork_name", "title")
            status = resp.get("status") or "—"
            price = safe_get(resp, "price", "kwork_price")
            desc = resp.get("description") or "—"
            duration = safe_get(resp, "kwork_duration", "duration")
            project_id = safe_get(resp, "want_id", "project_id")

            return (
                f"Предложение #{oid}\n"
                f"Название: {title}\n"
                f"Статус: {status}\n"
                f"Цена: {price} руб.\n"
                f"Срок: {duration} дн.\n"
                f"Проект: #{project_id}\n\n"
                f"Описание:\n{desc}"
            )

        return f"Предложение #{offer_id}:\n{json.dumps(resp, ensure_ascii=False, indent=2)}"

    @mcp.tool(annotations=ANNO_WRITE)
    async def submit_offer(
        project_id: int,
        title: str,
        description: str,
        price: int,
        duration_days: int,
        ctx: Context,
    ) -> str:
        """Отправить предложение на проект биржи Kwork.

        Когда использовать: для отклика на подходящий проект на бирже.
        Возвращает: ID предложения и подтверждение отправки.
        Связанные: get_project — изучить проект перед откликом, get_connects — проверить баланс коннектов.

        ВНИМАНИЕ: отправка предложения списывает один коннект.

        Параметры:
        - project_id: ID проекта на бирже
        - title: название вашего предложения (кворка)
        - description: подробное описание (минимум 150 символов)
        - price: цена в рублях
        - duration_days: срок выполнения в днях
        """
        validate_positive_id(project_id, "project_id")
        validate_not_empty(title, "title")
        validate_not_empty(description, "description")
        validate_positive_int(price, "price")
        validate_positive_int(duration_days, "duration_days")
        if len(description) < _MIN_DESCRIPTION_LENGTH:
            raise ToolError(
                f"Описание слишком короткое: {len(description)} символов. Минимум — {_MIN_DESCRIPTION_LENGTH} символов."
            )

        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_web_client()
        await session.rate_limit()

        async with api_guard("отправка предложения", session=session):
            result = await client.web.submit_exchange_offer(
                project_id=project_id,
                offer_type="custom",
                description=description,
                kwork_duration=duration_days,
                kwork_price=price,
                kwork_name=title,
            )

        resp_json = result.get("json")
        if isinstance(resp_json, dict):
            offer_id = resp_json.get("id") or resp_json.get("offer_id") or resp_json.get("response", {}).get("id")
            if offer_id:
                return (
                    f"Предложение #{offer_id} успешно отправлено на проект #{project_id}.\n"
                    f"Название: {title}\n"
                    f"Цена: {price} руб. | Срок: {duration_days} дн.\n\n"
                    f"Списан 1 коннект."
                )

        return (
            f"Предложение отправлено на проект #{project_id}.\n"
            f"Название: {title}\n"
            f"Цена: {price} руб. | Срок: {duration_days} дн.\n\n"
            f"Списан 1 коннект."
        )

    @mcp.tool(annotations=ANNO_DESTRUCTIVE)
    async def delete_offer(offer_id: int, ctx: Context) -> str:
        """Удалить ваше предложение с биржи Kwork.

        Когда использовать: для отмены отправленного предложения.
        Возвращает: подтверждение удаления.
        Связанные: list_my_offers — найти ID предложения для удаления.

        ВНИМАНИЕ: необратимое действие.
        """
        validate_positive_id(offer_id, "offer_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("удаление предложения", session=session):
            await client.delete_offer(id=offer_id, use_token=True)

        return f"Предложение #{offer_id} удалено."
