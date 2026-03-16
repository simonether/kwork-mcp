from __future__ import annotations

from fastmcp import FastMCP

from kwork_mcp.tools import (
    categories,
    dialogs,
    kworks,
    notifications,
    offers,
    orders,
    profile,
    projects,
)


def register_all(mcp: FastMCP) -> None:
    """Register every tool module with the MCP server."""
    profile.register(mcp)
    categories.register(mcp)
    notifications.register(mcp)
    orders.register(mcp)
    dialogs.register(mcp)
    kworks.register(mcp)
    projects.register(mcp)
    offers.register(mcp)
