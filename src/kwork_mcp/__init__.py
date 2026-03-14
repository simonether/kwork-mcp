from __future__ import annotations

import sys

from loguru import logger


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level} | {message}")

    from kwork_mcp.server import create_server

    server = create_server()
    server.run(transport="stdio")
