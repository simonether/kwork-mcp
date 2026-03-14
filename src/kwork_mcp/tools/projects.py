from __future__ import annotations

import json
from datetime import UTC, datetime

from mcp.server.fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager


def _fmt_date(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%d.%m.%Y")


def _truncate(text: str | None, limit: int = 200) -> str:
    if not text:
        return "—"
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _fmt_project_line(p) -> str:  # type: ignore[no-untyped-def]
    budget = f"{p.price} руб." if p.price else "не указан"
    max_price = f" (до {p.possible_price_limit} руб.)" if p.possible_price_limit else ""
    offers = p.offers if p.offers is not None else "?"
    hiring = f"{p.user_hired_percent}%" if p.user_hired_percent is not None else "?"
    return (
        f"[ID {p.id}] {p.title or '—'}\n"
        f"  Бюджет: {budget}{max_price} | Предложений: {offers} | Найм: {hiring}\n"
        f"  Дата: {_fmt_date(p.date_confirm)}\n"
        f"  {_truncate(p.description)}"
    )


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_projects(
        ctx: Context,
        category_id: int | None = None,
        price_from: int | None = None,
        price_to: int | None = None,
        hiring_from: int | None = None,
        max_offers: int | None = None,
        query: str | None = None,
        page: int = 1,
    ) -> str:
        """Список проектов на бирже Kwork с фильтрацией.

        Фильтры: категория, бюджет (от/до), процент найма, макс. предложений,
        текстовый поиск. Без фильтров — проекты из избранных категорий.
        """
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        categories: list[int] = [category_id] if category_id is not None else []

        async with api_guard("список проектов"):
            projects = await client.get_projects(
                categories_ids=categories,
                price_from=price_from,
                price_to=price_to,
                hiring_from=hiring_from,
                kworks_filter_to=max_offers,
                page=page,
                query=query,
            )

        if not projects:
            return "Проекты не найдены по заданным фильтрам."

        lines = [f"Найдено проектов: {len(projects)} (стр. {page})\n"]
        for p in projects:
            lines.append(_fmt_project_line(p))
        return "\n\n".join(lines)

    @mcp.tool()
    async def get_project(project_id: int, ctx: Context) -> str:
        """Подробная информация о проекте на бирже Kwork по его ID."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("получение проекта"):
            result = await client.project(id=project_id)

        resp = result.get("response", result)
        if isinstance(resp, dict):
            title = resp.get("name") or resp.get("title") or "—"
            desc = resp.get("description") or "—"
            price = resp.get("price") or resp.get("price_from") or "не указан"
            status = resp.get("status") or "—"
            user = resp.get("username") or "—"
            offers = resp.get("offers") or resp.get("user_offers_count") or 0
            hiring = resp.get("user_hired_percent")
            hiring_str = f"{hiring}%" if hiring is not None else "—"
            date = _fmt_date(resp.get("date_confirm"))

            return (
                f"Проект #{project_id}\n"
                f"Название: {title}\n"
                f"Статус: {status}\n"
                f"Заказчик: {user}\n"
                f"Бюджет: {price} руб.\n"
                f"Предложений: {offers} | Найм: {hiring_str}\n"
                f"Дата: {date}\n\n"
                f"Описание:\n{desc}"
            )

        return f"Проект #{project_id}:\n{json.dumps(resp, ensure_ascii=False, indent=2)}"

    @mcp.tool()
    async def search_projects(query: str, ctx: Context, page: int = 1) -> str:
        """Поиск проектов на бирже Kwork по текстовому запросу."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("поиск проектов"):
            projects = await client.get_projects(
                categories_ids=[],
                query=query,
                page=page,
            )

        if not projects:
            return f'По запросу "{query}" проекты не найдены.'

        lines = [f'Результаты по "{query}": {len(projects)} (стр. {page})\n']
        for p in projects:
            lines.append(_fmt_project_line(p))
        return "\n\n".join(lines)

    @mcp.tool()
    async def get_exchange_info(ctx: Context) -> str:
        """Общая статистика биржи Kwork: количество проектов, категории, лимиты."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("статистика биржи"):
            result = await client.exchange_info()

        resp = result.get("response", result)
        if isinstance(resp, dict):
            lines = ["Статистика биржи Kwork:\n"]
            for key, value in resp.items():
                if isinstance(value, (str, int, float, bool)):
                    lines.append(f"  {key}: {value}")
            return "\n".join(lines)

        return f"Статистика биржи:\n{json.dumps(resp, ensure_ascii=False, indent=2)}"
