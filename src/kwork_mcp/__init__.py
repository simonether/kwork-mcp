from __future__ import annotations

import sys
from collections.abc import Sequence

from kwork_mcp.config import secret_server_environment_present
from kwork_mcp.version import __version__

__all__ = ["__version__", "main"]


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        # Arguments select the human terminal commands (login, pending-writes,
        # resolve-write); the MCP client always starts the bare command. The
        # CLI never echoes argv, so a pasted secret does not reach the output.
        from kwork_mcp.bootstrap import main as cli_main

        cli_main(args)
        return
    if secret_server_environment_present():
        sys.stderr.write(
            "kwork-mcp отклонил секреты в окружении: логин, пароль, токен и прокси вводятся "
            "только в «kwork-mcp login», а не в конфиге MCP-клиента.\n"
        )
        raise SystemExit(2)
    from pydantic import ValidationError

    from kwork_mcp.config import AccountSelectionError, KworkConfig, load_server_config
    from kwork_mcp.server import create_server

    # Validator messages are static text without configured values.
    config: KworkConfig | None
    try:
        config = load_server_config()
    except AccountSelectionError as exc:
        sys.stderr.write(f"kwork-mcp: {exc}.\n")
        if exc.code is None:
            raise SystemExit(2) from None
        # Start anyway: the client shows a server that exits at once as a bare
        # failure, while every tool of a running one tells the agent the fix.
        sys.stderr.write("kwork-mcp: сервер запущен без аккаунта, каждый инструмент вернёт эту подсказку.\n")
        config = None
    except ValidationError as exc:
        problems = sorted(
            {
                f"KWORK_{str(error['loc'][0]).upper()}"
                if error.get("loc")
                else str(error["msg"]).removeprefix("Value error, ")
                for error in exc.errors(include_input=False)
            }
        )
        sys.stderr.write("kwork-mcp: некорректная конфигурация: " + "; ".join(problems) + ".\n")
        sys.stderr.write("Сначала выполните «kwork-mcp login»; справка: kwork-mcp --help.\n")
        raise SystemExit(2) from None
    except ValueError as exc:
        sys.stderr.write(f"kwork-mcp: некорректная конфигурация: {exc}.\n")
        raise SystemExit(2) from None
    server = create_server(config=config)
    # The banner also triggers FastMCP's PyPI update check: an unproxied
    # network call and a cache file outside KWORK_STATE_DIR on every start.
    server.run(transport="stdio", show_banner=False)
