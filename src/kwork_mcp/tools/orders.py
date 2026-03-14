from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager

_ORDER_STATUS_MAP: dict[int, str] = {
    0: "новый",
    1: "в работе",
    2: "на проверке",
    3: "завершён",
    4: "отменён",
    5: "арбитраж",
    6: "на доработке",
}


def _format_order_brief(order: dict[str, Any]) -> str:
    oid = order.get("id", "—")
    kwork_name = order.get("title") or order.get("kwork_title") or "—"
    raw_status = order.get("status")
    status = _ORDER_STATUS_MAP.get(raw_status, str(raw_status)) if raw_status is not None else "—"
    price = order.get("price") or order.get("total_price") or "—"
    buyer = order.get("payer_username") or order.get("username") or "—"
    return f"  #{oid} | {kwork_name} | {status} | {price} руб. | покупатель: {buyer}"


def _format_order_detail(data: dict[str, Any]) -> str:
    order = data.get("response") or data.get("order") or data
    oid = order.get("id", "—")
    kwork_name = order.get("title") or order.get("kwork_title") or "—"
    raw_status = order.get("status")
    status = _ORDER_STATUS_MAP.get(raw_status, str(raw_status)) if raw_status is not None else "—"
    price = order.get("price") or order.get("total_price") or "—"
    buyer = order.get("payer_username") or order.get("username") or "—"
    description = order.get("description") or order.get("instruction") or "—"
    deadline = order.get("date_must_be_done") or order.get("deadline") or "—"
    created = order.get("date_created") or order.get("created_at") or "—"

    lines = [
        f"Заказ #{oid}",
        f"Кворк: {kwork_name}",
        f"Статус: {status}",
        f"Цена: {price} руб.",
        f"Покупатель: {buyer}",
        f"Описание: {description}",
        f"Срок сдачи: {deadline}",
        f"Дата создания: {created}",
    ]
    return "\n".join(lines)


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_worker_orders(ctx: Context) -> str:
        """Получить список заказов текущего продавца (все статусы)."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_worker_orders"):
            data = await client.get_worker_orders()

        orders = data.get("response") or data.get("orders") or []
        if not orders:
            return "Заказов нет."

        if isinstance(orders, list):
            lines = ["Заказы продавца:"]
            for order in orders:
                if isinstance(order, dict):
                    lines.append(_format_order_brief(order))
                else:
                    lines.append(f"  {order}")
            return "\n".join(lines)

        return str(orders)

    @mcp.tool()
    async def get_order_details(order_id: int, ctx: Context) -> str:
        """Получить подробную информацию о заказе по его ID."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_order_details"):
            data = await client.get_order_details(order_id=order_id)

        return _format_order_detail(data)

    @mcp.tool()
    async def send_order_for_approval(order_id: int, ctx: Context) -> str:
        """Отправить заказ на проверку покупателю.

        ВНИМАНИЕ: это действие отправляет выполненную работу на проверку.
        Убедитесь, что работа полностью завершена перед вызовом.
        """
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("send_order_for_approval"):
            data = await client.send_order_for_approval(order_id=order_id)

        if data.get("success") or data.get("status") == "ok":
            return f"Заказ #{order_id} отправлен на проверку покупателю."

        error = data.get("error") or data.get("message") or data
        return f"Не удалось отправить заказ #{order_id}: {error}"
