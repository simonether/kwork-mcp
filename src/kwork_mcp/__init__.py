from __future__ import annotations

import sys
from collections.abc import Sequence

from kwork_mcp.config import secret_server_environment_present
from kwork_mcp.version import __version__

__all__ = ["__version__", "main"]


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        sys.stderr.write("kwork-mcp не принимает аргументы; используйте только безопасную environment-конфигурацию.\n")
        raise SystemExit(2)
    if secret_server_environment_present():
        sys.stderr.write(
            "kwork-mcp отклонил secret-bearing environment; выполните kwork-mcp-bootstrap "
            "и запускайте MCP только с account ID и защищённым store.\n"
        )
        raise SystemExit(2)
    from kwork_mcp.server import create_server

    server = create_server()
    server.run(transport="stdio")
