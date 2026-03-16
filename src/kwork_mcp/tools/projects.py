from __future__ import annotations

import json
from typing import Any

from fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import (
    ANNO_READ,
    format_date,
    truncate,
    unwrap_response,
    validate_not_empty,
    validate_page,
    validate_positive_id,
)


def _fmt_project_line(p: Any) -> str:
    budget = f"{p.price} руб." if p.price else "не указан"
    max_price = f" (до {p.possible_price_limit} руб.)" if p.possible_price_limit else ""
    offers = p.offers if p.offers is not None else "?"
    hiring = f"{p.user_hired_percent}%" if p.user_hired_percent is not None else "?"
    return (
        f"[ID {p.id}] {p.title or '—'}\n"
        f"  Бюджет: {budget}{max_price} | Предложений: {offers} | Найм: {hiring}\n"
        f"  Дата: {format_date(p.date_confirm)}\n"
        f"  {truncate(p.description)}"
    )


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ANNO_READ)
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

        Когда использовать: для поиска подходящих проектов на бирже.
        Возвращает: список проектов с бюджетом, количеством предложений и описанием.
        Связанные: get_project — подробности проекта, submit_offer — отправить предложение.

        Фильтры: категория, бюджет (от/до), процент найма, макс. предложений,
        текстовый поиск. Без фильтров — проекты из избранных категорий.
        """
        validate_page(page)
        if category_id is not None:
            validate_positive_id(category_id, "category_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()

        categories: list[int] = [category_id] if category_id is not None else []

        async with api_guard("список проектов", session=session):
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

    @mcp.tool(annotations=ANNO_READ)
    async def get_project(project_id: int, ctx: Context) -> str:
        """Подробная информация о проекте на бирже Kwork по его ID.

        Когда использовать: для изучения конкретного проекта перед отправкой предложения.
        Возвращает: название, статус, заказчик, бюджет, описание, количество предложений.
        Связанные: submit_offer — отправить предложение на проект.
        """
        validate_positive_id(project_id, "project_id")
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("получение проекта", session=session):
            result = await client.project(id=project_id, use_token=True)

        resp = unwrap_response(result)
        if isinstance(resp, dict):
            title = resp.get("name") or resp.get("title") or "—"
            desc = resp.get("description") or "—"
            price = resp.get("price") or resp.get("price_from") or "не указан"
            status = resp.get("status") or "—"
            user = resp.get("username") or "—"
            offers = resp.get("offers") or resp.get("user_offers_count") or 0
            hiring = resp.get("user_hired_percent")
            hiring_str = f"{hiring}%" if hiring is not None else "—"
            date = format_date(resp.get("date_confirm"))

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

    @mcp.tool(annotations=ANNO_READ)
    async def search_projects(query: str, ctx: Context, page: int = 1) -> str:
        """Поиск проектов на бирже Kwork по текстовому запросу.

        Когда использовать: для поиска проектов по ключевым словам.
        Возвращает: список найденных проектов с бюджетом и описанием.
        Связанные: list_projects — расширенная фильтрация, get_project — подробности.
        """
        validate_not_empty(query, "query")
        validate_page(page)
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()

        async with api_guard("поиск проектов", session=session):
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

    @mcp.tool(annotations=ANNO_READ)
    async def get_exchange_info(ctx: Context) -> str:
        """Общая статистика биржи Kwork: количество проектов, категории, лимиты.

        Когда использовать: для получения общей информации о бирже перед работой с проектами.
        Возвращает: статистику биржи в виде ключ-значение.
        Связанные: list_projects — список проектов, list_categories — дерево категорий.
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        await session.rate_limit()

        async with api_guard("статистика биржи", session=session):
            data = await session.get_exchange_info()

        lines = ["Статистика биржи Kwork:\n"]
        for key, value in data.items():
            if isinstance(value, (str, int, float, bool)):
                lines.append(f"  {key}: {value}")
        return "\n".join(lines)
