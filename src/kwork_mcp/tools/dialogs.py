from __future__ import annotations

from datetime import UTC, datetime

from mcp.server.fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager


def _ts_to_str(ts: int | float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%d.%m.%Y %H:%M")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_dialogs(page: int = 1, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Получить список диалогов (личных переписок) на Kwork.

        Возвращает список собеседников с последним сообщением и счётчиком непрочитанных.
        """
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_dialogs"):
            dialogs = await client.get_dialogs_page(page=page)

        if not dialogs:
            return "Диалогов нет."

        lines = [f"Диалоги (стр. {page}):"]
        for d in dialogs:
            username = d.username or "—"
            last_msg = (d.last_message or "—")[:80]
            unread = d.unread_count or 0
            time_str = _ts_to_str(d.time)
            unread_mark = f" [{unread} непрочит.]" if unread else ""
            lines.append(f"  @{username}{unread_mark} ({time_str})")
            lines.append(f"    {last_msg}")
        return "\n".join(lines)

    @mcp.tool()
    async def get_dialog(username: str, page: int = 1, ctx: Context = None) -> str:  # type: ignore[assignment]
        """Получить сообщения из диалога с пользователем по его username.

        Сообщения выводятся в хронологическом порядке (старые сверху).
        """
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_dialog"):
            messages, paging = await client.get_dialog_with_user_page(
                username=username,
                page=page,
            )

        if not messages:
            return f"Сообщений с @{username} не найдено."

        total_pages = paging.get("pages", 1)
        lines = [f"Диалог с @{username} (стр. {page}/{total_pages}):"]

        for msg in reversed(messages):
            sender = msg.from_username or "—"
            time_str = _ts_to_str(msg.time)
            text = msg.message or "—"
            lines.append(f"  [{time_str}] @{sender}: {text}")

        return "\n".join(lines)

    @mcp.tool()
    async def send_message(user_id: int, text: str, ctx: Context) -> str:
        """Отправить личное сообщение пользователю Kwork по его user_id.

        ВНИМАНИЕ: сообщение будет реально отправлено пользователю на Kwork.
        Проверьте текст и user_id перед вызовом.
        """
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("send_message"):
            data = await client.send_message(user_id=user_id, text=text)

        if data.get("success") or data.get("status") == "ok":
            return f"Сообщение отправлено пользователю (ID {user_id})."

        error = data.get("error") or data.get("message") or data
        return f"Не удалось отправить сообщение: {error}"

    @mcp.tool()
    async def mark_dialog_read(user_id: int, ctx: Context) -> str:
        """Пометить все сообщения в диалоге с пользователем как прочитанные."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("mark_dialog_read"):
            data = await client.inbox_read(user_id=user_id)

        if data.get("success") or data.get("status") == "ok":
            return f"Диалог с пользователем (ID {user_id}) отмечен как прочитанный."

        error = data.get("error") or data.get("message") or data
        return f"Не удалось отметить диалог прочитанным: {error}"
