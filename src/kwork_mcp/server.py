from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from loguru import logger

from kwork_mcp.config import KworkConfig
from kwork_mcp.session import KworkSessionManager


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    config = KworkConfig()
    session = KworkSessionManager(config)
    logger.info("Kwork MCP server starting")
    try:
        yield {"session": session}
    finally:
        await session.close()
        logger.info("Kwork MCP server stopped")


mcp = FastMCP("kwork", lifespan=lifespan, mask_error_details=True)


def create_server() -> FastMCP:
    from kwork_mcp.tools import register_all

    register_all(mcp)
    return mcp
