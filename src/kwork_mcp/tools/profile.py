from __future__ import annotations

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import (
    ANNO_READ,
    safe_get,
    unwrap_response,
    validate_not_empty,
    validate_one_of,
    validate_positive_id,
)


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ANNO_READ)
    async def get_me(ctx: Context) -> str:
        """Получить информацию о текущем пользователе Kwork (профиль, рейтинг, баланс).

        Когда использовать: для проверки своего профиля, баланса, рейтинга.
        Возвращает: имя, рейтинг, отзывы, уровень, баланс, количество заказов.
        Связанные: get_connects — баланс коннектов.
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_me", session=session):
            actor = await client.get_me()

        lines = [
            f"Имя пользователя: {actor.username or '—'}",
            f"Полное имя: {actor.fullname or '—'}",
            f"Рейтинг: {actor.rating or 0}",
            f"Положительных отзывов: {actor.good_reviews or 0}",
            f"Отрицательных отзывов: {actor.bad_reviews or 0}",
            f"Уровень: {actor.level_description or '—'}",
            f"Специализация: {actor.specialization or '—'}",
            f"Баланс: {actor.free_amount or 0} {actor.currency or 'RUB'}",
            f"В холде: {actor.hold_amount or 0} {actor.currency or 'RUB'}",
            f"Выполненных заказов: {actor.completed_orders_count or 0}",
            f"Статус: {actor.worker_status or '—'}",
        ]
        return "\n".join(lines)

    @mcp.tool(annotations=ANNO_READ)
    async def get_connects(ctx: Context) -> str:
        """Получить информацию о коннектах (для отклика на проекты биржи).

        Когда использовать: перед отправкой предложения, чтобы проверить баланс коннектов.
        Возвращает: количество активных и общее количество коннектов.
        Связанные: submit_offer — отправка предложения (списывает 1 коннект).
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_connects", session=session):
            connects = await client.get_connects()

        lines = [
            f"Активных коннектов: {connects.active_connects or 0}",
            f"Всего коннектов: {connects.all_connects or 0}",
        ]
        return "\n".join(lines)

    @mcp.tool(annotations=ANNO_READ)
    async def get_user_info(
        ctx: Context,
        user_id: int | None = None,
        username: str | None = None,
    ) -> str:
        """Получить публичную информацию о пользователе Kwork по ID или username.

        Когда использовать: для проверки профиля, получения user_id по username
        перед отправкой сообщения, или просмотра рейтинга собеседника.
        Возвращает: ID, имя, рейтинг, отзывы, специализация, онлайн-статус.
        Связанные: search_users — поиск пользователя по запросу,
        send_message — отправить сообщение (требует user_id или username).
        """
        validate_one_of(user_id=user_id, username=username)

        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()

        if user_id is not None:
            validate_positive_id(user_id, "user_id")
            await session.rate_limit()
            async with api_guard("get_user_info", session=session):
                user = await client.get_user(user_id)
            lines = [
                f"ID пользователя: {user.id or '—'}",
                f"Имя пользователя: {user.username or '—'}",
                f"Полное имя: {user.fullname or '—'}",
                f"Рейтинг: {user.rating or 0}",
                f"Положительных отзывов: {user.good_reviews or 0}",
                f"Отрицательных отзывов: {user.bad_reviews or 0}",
                f"Уровень: {user.level_description or '—'}",
                f"Специализация: {user.specialization or '—'}",
                f"Описание: {user.description or '—'}",
                f"Онлайн: {'да' if user.online else 'нет'}",
            ]
            return "\n".join(lines)

        # Поиск по username
        validate_not_empty(username, "username")
        username = username.lstrip("@").strip()
        await session.rate_limit()
        async with api_guard("get_user_info", session=session):
            data = await client.user_by_username(username=username)

        user_data = unwrap_response(data)
        if not isinstance(user_data, dict) or not user_data.get("id"):
            raise ToolError(
                f"Пользователь @{username} не найден. Проверьте username или используйте search_users для поиска."
            )

        lines = [
            f"ID пользователя: {safe_get(user_data, 'id')}",
            f"Имя пользователя: {safe_get(user_data, 'username')}",
            f"Полное имя: {safe_get(user_data, 'fullname')}",
            f"Рейтинг: {safe_get(user_data, 'rating', default=0)}",
            f"Положительных отзывов: {safe_get(user_data, 'good_reviews', default=0)}",
            f"Отрицательных отзывов: {safe_get(user_data, 'bad_reviews', default=0)}",
            f"Уровень: {safe_get(user_data, 'level_description')}",
            f"Специализация: {safe_get(user_data, 'specialization')}",
            f"Описание: {safe_get(user_data, 'description')}",
            f"Онлайн: {'да' if user_data.get('online') else 'нет'}",
        ]
        return "\n".join(lines)

    @mcp.tool(annotations=ANNO_READ)
    async def search_users(query: str, ctx: Context) -> str:
        """Поиск пользователей Kwork по имени или username.

        Когда использовать: для поиска пользователя, если не известен точный username
        или user_id.
        Возвращает: список найденных пользователей с ID, username, рейтингом.
        Связанные: get_user_info — подробный профиль найденного пользователя.
        """
        validate_not_empty(query, "query")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("search_users", session=session):
            data = await client.user_search(query=query)

        resp = unwrap_response(data)

        if isinstance(resp, list):
            users = resp
        elif isinstance(resp, dict):
            users = resp.get("users") or resp.get("items") or []
            if not isinstance(users, list):
                users = []
        else:
            users = []

        if not users:
            return f"Пользователи по запросу «{query}» не найдены."

        lines = [f"Результаты поиска «{query}» ({len(users)}):"]
        for u in users:
            if not isinstance(u, dict):
                continue
            uid = u.get("id", "—")
            uname = u.get("username", "—")
            fullname = u.get("fullname", "")
            rating = u.get("rating", "—")
            spec = u.get("specialization") or u.get("profession") or ""
            name_part = f" ({fullname})" if fullname else ""
            spec_part = f" | {spec}" if spec else ""
            lines.append(f"  @{uname}{name_part} [ID: {uid}] | рейтинг: {rating}{spec_part}")

        return "\n".join(lines)
