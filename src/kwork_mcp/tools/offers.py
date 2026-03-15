from __future__ import annotations

import json

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager

_MIN_DESCRIPTION_LENGTH = 150


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_my_offers(ctx: Context) -> str:
        """Список ваших предложений (офферов) на бирже Kwork."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("список предложений"):
            result = await client.offers(use_token=True)

        resp = result.get("response", result)

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
            project_name = offer.get("name") or offer.get("want_name") or offer.get("title") or "—"
            status = offer.get("status") or "—"
            price = offer.get("price") or offer.get("kwork_price") or "—"
            lines.append(f"[ID {oid}] {project_name}\n  Статус: {status} | Цена: {price} руб.")
        return "\n\n".join(lines)

    @mcp.tool()
    async def get_offer(offer_id: int, ctx: Context) -> str:
        """Подробная информация о вашем предложении на бирже Kwork."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("получение предложения"):
            result = await client.offer(id=offer_id, use_token=True)

        resp = result.get("response", result)

        if isinstance(resp, dict):
            oid = resp.get("id", offer_id)
            title = resp.get("name") or resp.get("kwork_name") or resp.get("title") or "—"
            status = resp.get("status") or "—"
            price = resp.get("price") or resp.get("kwork_price") or "—"
            desc = resp.get("description") or "—"
            duration = resp.get("kwork_duration") or resp.get("duration") or "—"
            project_id = resp.get("want_id") or resp.get("project_id") or "—"

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

    @mcp.tool()
    async def submit_offer(
        project_id: int,
        title: str,
        description: str,
        price: int,
        duration_days: int,
        ctx: Context,
    ) -> str:
        """Отправить предложение на проект биржи Kwork.

        ВНИМАНИЕ: отправка предложения списывает один коннект.

        Параметры:
        - project_id: ID проекта на бирже
        - title: название вашего предложения (кворка)
        - description: подробное описание (минимум 150 символов)
        - price: цена в рублях
        - duration_days: срок выполнения в днях
        """
        if len(description) < _MIN_DESCRIPTION_LENGTH:
            raise ToolError(
                f"Описание слишком короткое: {len(description)} символов. Минимум — {_MIN_DESCRIPTION_LENGTH} символов."
            )

        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_web_client()
        await session.rate_limit()

        async with api_guard("отправка предложения"):
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

    @mcp.tool()
    async def delete_offer(offer_id: int, ctx: Context) -> str:
        """Удалить ваше предложение с биржи Kwork."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("удаление предложения"):
            await client.delete_offer(id=offer_id, use_token=True)

        return f"Предложение #{offer_id} удалено."
