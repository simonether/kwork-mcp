from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def get_me(ctx: Context) -> str:
        """Получить информацию о текущем пользователе Kwork (профиль, рейтинг, баланс)."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_me"):
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

    @mcp.tool()
    async def get_connects(ctx: Context) -> str:
        """Получить информацию о коннектах (для отклика на проекты биржи)."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_connects"):
            connects = await client.get_connects()

        lines = [
            f"Активных коннектов: {connects.active_connects or 0}",
            f"Всего коннектов: {connects.all_connects or 0}",
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def get_user_info(user_id: int, ctx: Context) -> str:
        """Получить публичную информацию о пользователе Kwork по его ID."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_user_info"):
            user = await client.get_user(user_id)

        lines = [
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
