from __future__ import annotations

from datetime import UTC, datetime

from mcp.server.fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_notifications(ctx: Context) -> str:
        """Получить список уведомлений текущего пользователя Kwork."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_notifications"):
            data = await client.get_notifications()

        notifications = data.get("notifications", data.get("data", []))
        if not notifications:
            return "Уведомлений нет."

        lines: list[str] = []
        if isinstance(notifications, list):
            for notif in notifications:
                if isinstance(notif, dict):
                    text = notif.get("text", notif.get("message", "—"))
                    raw_date = notif.get("date", notif.get("created_at"))
                    if isinstance(raw_date, (int, float)):
                        date_str = datetime.fromtimestamp(
                            raw_date,
                            tz=UTC,
                        ).strftime("%d.%m.%Y %H:%M")
                    elif raw_date:
                        date_str = str(raw_date)
                    else:
                        date_str = "—"
                    lines.append(f"[{date_str}] {text}")
                else:
                    lines.append(str(notif))
        else:
            lines.append(str(notifications))
        return "\n".join(lines)
