from __future__ import annotations

from fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import (
    ANNO_READ,
    ANNO_WRITE,
    check_success,
    extract_error,
    format_timestamp,
    truncate,
    validate_not_empty,
    validate_page,
    validate_positive_id,
)


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ANNO_READ)
    async def list_dialogs(ctx: Context, page: int = 1) -> str:
        """Получить список диалогов (личных переписок) на Kwork.

        Когда использовать: для просмотра списка собеседников и последних сообщений.
        Возвращает: список собеседников с последним сообщением и счётчиком непрочитанных.
        Связанные: get_dialog — сообщения конкретного диалога.
        """
        validate_page(page)
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_dialogs", session=session):
            dialogs = await client.get_dialogs_page(page=page)

        if not dialogs:
            return "Диалогов нет."

        lines = [f"Диалоги (стр. {page}):"]
        for d in dialogs:
            username = d.username or "—"
            last_msg = truncate(d.last_message, 80)
            unread = d.unread_count or 0
            time_str = format_timestamp(d.time)
            unread_mark = f" [{unread} непрочит.]" if unread else ""
            lines.append(f"  @{username}{unread_mark} ({time_str})")
            lines.append(f"    {last_msg}")
        return "\n".join(lines)

    @mcp.tool(annotations=ANNO_READ)
    async def get_dialog(username: str, ctx: Context, page: int = 1) -> str:
        """Получить сообщения из диалога с пользователем по его username.

        Когда использовать: для чтения переписки с конкретным пользователем.
        Возвращает: сообщения в хронологическом порядке (старые сверху).
        Связанные: list_dialogs — список всех диалогов, send_message — отправить ответ.
        """
        validate_not_empty(username, "username")
        validate_page(page)
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_dialog", session=session):
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
            time_str = format_timestamp(msg.time)
            text = msg.message or "—"
            lines.append(f"  [{time_str}] @{sender}: {text}")

        return "\n".join(lines)

    @mcp.tool(annotations=ANNO_WRITE)
    async def send_message(user_id: int, text: str, ctx: Context) -> str:
        """Отправить личное сообщение пользователю Kwork по его user_id.

        Когда использовать: для отправки сообщения конкретному пользователю.
        Возвращает: подтверждение отправки или сообщение об ошибке.
        Связанные: get_dialog — прочитать переписку перед ответом.

        ВНИМАНИЕ: сообщение будет реально отправлено пользователю на Kwork.
        Проверьте текст и user_id перед вызовом.
        """
        validate_positive_id(user_id, "user_id")
        validate_not_empty(text, "text")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("send_message", session=session):
            data = await client.send_message(user_id=user_id, text=text)

        if check_success(data):
            return f"Сообщение отправлено пользователю (ID {user_id})."

        return f"Не удалось отправить сообщение: {extract_error(data)}"

    @mcp.tool(annotations=ANNO_WRITE)
    async def mark_dialog_read(user_id: int, ctx: Context) -> str:
        """Пометить все сообщения в диалоге с пользователем как прочитанные.

        Когда использовать: после прочтения диалога, чтобы убрать счётчик непрочитанных.
        Возвращает: подтверждение или сообщение об ошибке.
        Связанные: list_dialogs — увидеть непрочитанные диалоги.
        """
        validate_positive_id(user_id, "user_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("mark_dialog_read", session=session):
            data = await client.inbox_read(user_id=user_id)

        if check_success(data):
            return f"Диалог с пользователем (ID {user_id}) отмечен как прочитанный."

        return f"Не удалось отметить диалог прочитанным: {extract_error(data)}"
