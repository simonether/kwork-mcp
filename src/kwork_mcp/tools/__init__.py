from __future__ import annotations

from fastmcp import FastMCP

from kwork_mcp.tools import read_tools, write_tools


def register_all(mcp: FastMCP) -> None:
    """Register the stable 1.0 read surface and durable write protocol."""
    read_tools.register(mcp)
    write_tools.register(mcp)
