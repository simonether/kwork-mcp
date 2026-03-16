from __future__ import annotations

from fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager
from kwork_mcp.utils import ANNO_READ, unwrap_response


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=ANNO_READ)
    async def list_categories(ctx: Context) -> str:
        """Получить дерево всех категорий Kwork (родительские, подкатегории, категории).

        Когда использовать: для выбора категории при фильтрации проектов или создании кворка.
        Возвращает: иерархическое дерево категорий с ID.
        Связанные: list_projects — фильтрация проектов по категории.
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_categories", session=session):
            parents = await client.get_categories()

        lines: list[str] = []
        for parent in parents:
            lines.append(f"[{parent.id}] {parent.name or '—'}")
            for sub in parent.subcategories or []:
                lines.append(f"  [{sub.id}] {sub.name or '—'}")
                for cat in sub.subcategories or []:
                    lines.append(f"    [{cat.id}] {cat.name or '—'}")
        return "\n".join(lines) if lines else "Категории не найдены."

    @mcp.tool(annotations=ANNO_READ)
    async def get_favorite_categories(ctx: Context) -> str:
        """Получить список избранных категорий текущего пользователя.

        Когда использовать: для просмотра категорий, на которые подписан пользователь.
        Возвращает: список избранных категорий с ID и названием.
        Связанные: list_categories — полное дерево категорий.
        """
        session: KworkSessionManager = ctx.lifespan_context["session"]
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_favorite_categories", session=session):
            data = await client.favorite_categories()

        response = unwrap_response(data)
        if isinstance(response, dict):
            categories = response.get("categories") or response.get("data") or []
        elif isinstance(response, list):
            categories = response
        else:
            categories = []
        if not categories:
            return "Избранных категорий нет."

        lines: list[str] = []
        if isinstance(categories, list):
            for cat in categories:
                if isinstance(cat, dict):
                    cat_id = cat.get("id", "—")
                    name = cat.get("name", "—")
                    lines.append(f"[{cat_id}] {name}")
                else:
                    lines.append(str(cat))
        else:
            lines.append(str(categories))
        return "\n".join(lines)
