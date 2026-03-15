from __future__ import annotations

import json
from datetime import UTC, datetime
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


def _fmt_ts(value: Any) -> str:
    """Format a unix timestamp or date string to human-readable date."""
    if not value:
        return "—"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).strftime("%d.%m.%Y %H:%M")
    return str(value)


def _buyer_name(order: dict[str, Any]) -> str:
    payer = order.get("payer")
    if isinstance(payer, dict):
        return payer.get("username", "—")
    return order.get("payer_username") or order.get("username") or "—"


def _order_title(order: dict[str, Any]) -> str:
    return order.get("display_title") or order.get("kwork_title") or order.get("title") or "—"


def _order_status(order: dict[str, Any]) -> str:
    raw = order.get("status")
    if raw is None:
        return "—"
    return _ORDER_STATUS_MAP.get(raw, str(raw))


def _format_order_brief(order: dict[str, Any]) -> str:
    oid = order.get("id", "—")
    price = order.get("price") or order.get("total_price") or "—"
    time_left = order.get("time_left")
    suffix = f" | осталось: {time_left}" if time_left else ""
    return f"  #{oid} | {_order_title(order)} | {_order_status(order)} | {price} руб. | покупатель: {_buyer_name(order)}{suffix}"


_STAGE_STATUS_MAP: dict[int, str] = {
    1: "ожидает",
    2: "в работе",
    3: "завершён",
}


def _format_order_detail(data: dict[str, Any]) -> str:
    response = data.get("response") or data

    # getOrderDetails returns {details, stages, key_tracks} — supplementary info
    details = response.get("details") if isinstance(response, dict) else None
    stages = response.get("stages") if isinstance(response, dict) else None
    key_tracks = response.get("key_tracks") if isinstance(response, dict) else None

    lines: list[str] = []

    # Description from details
    if isinstance(details, dict):
        desc = details.get("description") or "—"
        lines.append(f"Описание: {desc}")

    # Stages
    if isinstance(stages, list) and stages:
        lines.append(f"\nЭтапы ({len(stages)}):")
        for s in stages:
            if not isinstance(s, dict):
                continue
            num = s.get("number", "?")
            title = s.get("title", "—")
            status = _STAGE_STATUS_MAP.get(s.get("status", -1), str(s.get("status", "?")))
            price = s.get("price", "?")
            progress = s.get("progress")
            progress_str = f" | {progress}%" if progress is not None else ""
            lines.append(f"  {num}. {title} — {status} | {price} руб.{progress_str}")

    # Key tracks (timeline)
    if isinstance(key_tracks, list) and key_tracks:
        lines.append(f"\nИстория ({len(key_tracks)}):")
        for track in key_tracks[:10]:  # Last 10 events
            if not isinstance(track, dict):
                continue
            title = track.get("title", "—")
            ts = _fmt_ts(track.get("created_at"))
            lines.append(f"  [{ts}] {title}")
        if len(key_tracks) > 10:
            lines.append(f"  ... и ещё {len(key_tracks) - 10}")

    if not lines:
        return f"Детали заказа:\n{json.dumps(response, ensure_ascii=False, indent=2)}"

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

        response = data.get("response") or data
        if isinstance(response, dict):
            orders = response.get("orders") or []
            paging = response.get("paging", {})
            filter_counts = response.get("filter_counts", {})
        elif isinstance(response, list):
            orders = response
            paging = {}
            filter_counts = {}
        else:
            return "Заказов нет."

        if not orders:
            return "Заказов нет."

        total = paging.get("total") or len(orders)
        lines = [f"Заказы продавца (всего: {total}):"]

        if filter_counts:
            counts = ", ".join(f"{k}: {v}" for k, v in filter_counts.items() if v)
            lines.append(f"  ({counts})")

        for order in orders:
            if isinstance(order, dict):
                lines.append(_format_order_brief(order))
            else:
                lines.append(f"  {order}")

        return "\n".join(lines)

    @mcp.tool()
    async def get_order_details(order_id: int, ctx: Context) -> str:
        """Получить подробную информацию о заказе по его ID."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_order_details"):
            data = await client.get_order_details(orderId=order_id)

        return _format_order_detail(data)

    @mcp.tool()
    async def send_order_for_approval(order_id: int, ctx: Context) -> str:
        """Отправить выполненную работу на проверку покупателю. Необратимое действие."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("send_order_for_approval"):
            data = await client.send_order_for_approval(order_id=order_id)

        if data.get("success") or data.get("status") == "ok":
            return f"Заказ #{order_id} отправлен на проверку покупателю."

        error = data.get("error") or data.get("message") or data
        return f"Не удалось отправить заказ #{order_id}: {error}"
