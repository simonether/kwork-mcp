from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP

from kwork_mcp.errors import api_guard
from kwork_mcp.session import KworkSessionManager


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def list_categories(ctx: Context) -> str:
        """Получить дерево всех категорий Kwork (родительские, подкатегории, категории)."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("list_categories"):
            parents = await client.get_categories()

        lines: list[str] = []
        for parent in parents:
            lines.append(f"[{parent.id}] {parent.name or '—'}")
            for sub in parent.subcategories or []:
                lines.append(f"  [{sub.id}] {sub.name or '—'}")
                for cat in sub.subcategories or []:
                    lines.append(f"    [{cat.id}] {cat.name or '—'}")
        return "\n".join(lines) if lines else "Категории не найдены."

    @mcp.tool()
    async def get_favorite_categories(ctx: Context) -> str:
        """Получить список избранных категорий текущего пользователя."""
        session: KworkSessionManager = ctx.request_context.lifespan_context
        client = await session.ensure_client()
        await session.rate_limit()
        async with api_guard("get_favorite_categories"):
            data = await client.favorite_categories()

        categories = data.get("categories", data.get("data", []))
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
