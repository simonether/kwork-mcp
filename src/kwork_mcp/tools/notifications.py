from __future__ import annotations

from fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import ANNO_READ, format_timestamp, unwrap_response


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ANNO_READ)
    async def list_notifications(ctx: Context) -> str:
        """Получить список уведомлений текущего пользователя Kwork.

        Когда использовать: для просмотра последних уведомлений (заказы, сообщения, системные).
        Возвращает: список уведомлений с датой и текстом.
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_notifications", session=session):
            data = await client.get_notifications()

        response = unwrap_response(data)
        if isinstance(response, dict):
            notifications = response.get("notifications") or response.get("data") or []
        elif isinstance(response, list):
            notifications = response
        else:
            notifications = []
        if not notifications:
            return "Уведомлений нет."

        lines: list[str] = []
        if isinstance(notifications, list):
            for notif in notifications:
                if isinstance(notif, dict):
                    text = notif.get("text", notif.get("message", "—"))
                    raw_date = notif.get("date", notif.get("created_at"))
                    date_str = format_timestamp(raw_date)
                    lines.append(f"[{date_str}] {text}")
                else:
                    lines.append(str(notif))
        else:
            lines.append(str(notifications))
        return "\n".join(lines)
