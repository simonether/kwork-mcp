from __future__ import annotations

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import (
    ANNO_DESTRUCTIVE,
    ANNO_READ,
    ANNO_WRITE,
    check_success,
    extract_error,
    format_timestamp,
    truncate,
    unwrap_response,
    validate_not_empty,
    validate_one_of,
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
            uid = d.user_id or "—"
            last_msg = truncate(d.last_message, 80)
            unread = d.unread_count or 0
            time_str = format_timestamp(d.time)
            unread_mark = f" [{unread} непрочит.]" if unread else ""
            lines.append(f"  @{username} (ID: {uid}){unread_mark} ({time_str})")
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
            msg_id = msg.message_id or "—"
            sender = msg.from_username or "—"
            time_str = format_timestamp(msg.time)
            text = msg.message or "—"
            lines.append(f"  [#{msg_id}] [{time_str}] @{sender}: {text}")

        return "\n".join(lines)

    @mcp.tool(annotations=ANNO_WRITE)
    async def send_message(
        text: str,
        ctx: Context,
        user_id: int | None = None,
        username: str | None = None,
    ) -> str:
        """Отправить личное сообщение пользователю Kwork по user_id или username.

        Когда использовать: для отправки сообщения конкретному пользователю.
        Можно указать user_id или username — при username система автоматически
        определит user_id.
        Возвращает: подтверждение отправки или сообщение об ошибке.
        Связанные: get_dialog — прочитать переписку перед ответом,
        get_user_info — узнать user_id по username.

        ВНИМАНИЕ: сообщение будет реально отправлено пользователю на Kwork.
        """
        validate_one_of(user_id=user_id, username=username)
        validate_not_empty(text, "text")

        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()

        resolved_id = user_id
        resolved_name = username

        if user_id is not None:
            validate_positive_id(user_id, "user_id")
        else:
            validate_not_empty(username, "username")
            username = username.lstrip("@").strip()
            resolved_name = username
            await session.rate_limit()
            async with api_guard("send_message_resolve", session=session):
                data = await client.user_by_username(username=username)
            user_data = unwrap_response(data)
            if not isinstance(user_data, dict) or not user_data.get("id"):
                raise ToolError(f"Пользователь @{username} не найден. Проверьте username или используйте search_users.")
            resolved_id = user_data["id"]

        await session.rate_limit()
        async with api_guard("send_message", session=session):
            data = await client.send_message(user_id=resolved_id, text=text)

        if check_success(data):
            if resolved_name:
                return f"Сообщение отправлено @{resolved_name} (ID {resolved_id})."
            return f"Сообщение отправлено пользователю (ID {resolved_id})."

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

    @mcp.tool(annotations=ANNO_WRITE)
    async def edit_message(message_id: int, text: str, ctx: Context) -> str:
        """Редактировать отправленное сообщение по его message_id.

        Когда использовать: для исправления опечаток или обновления текста
        отправленного сообщения.
        Возвращает: подтверждение редактирования или сообщение об ошибке.
        Связанные: get_dialog — найти message_id сообщения,
        delete_message — удалить вместо редактирования.

        ВНИМАНИЕ: можно редактировать только свои сообщения.
        """
        validate_positive_id(message_id, "message_id")
        validate_not_empty(text, "text")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("edit_message", session=session):
            data = await client.inbox_edit(id=message_id, body={"text": text})

        if check_success(data):
            return f"Сообщение #{message_id} отредактировано."

        return f"Не удалось отредактировать сообщение: {extract_error(data)}"

    @mcp.tool(annotations=ANNO_DESTRUCTIVE)
    async def delete_message(message_id: int, ctx: Context) -> str:
        """Удалить отправленное сообщение по его message_id.

        Когда использовать: для удаления ошибочно отправленного сообщения.
        Возвращает: подтверждение удаления или сообщение об ошибке.
        Связанные: get_dialog — найти message_id сообщения,
        edit_message — отредактировать вместо удаления.

        ВНИМАНИЕ: необратимое действие. Удалить можно только свои сообщения.
        """
        validate_positive_id(message_id, "message_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("delete_message", session=session):
            data = await client.delete_message(message_id)

        if check_success(data):
            return f"Сообщение #{message_id} удалено."

        return f"Не удалось удалить сообщение: {extract_error(data)}"
