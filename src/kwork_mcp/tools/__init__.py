from __future__ import annotations

from fastmcp import FastMCP

from kwork_mcp.tools import read_tools, write_tools


def register_all(mcp: FastMCP, *, writes: str | None = None) -> None:
    """Register the read surface and the write protocol allowed by KWORK_WRITES."""
    read_tools.register(mcp)
    write_tools.register(mcp, writes=writes)
