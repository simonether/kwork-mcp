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
    from pydantic import ValidationError

    from kwork_mcp.config import KworkConfig, validate_steady_state_server_config
    from kwork_mcp.server import create_server

    # Validator messages are static text without configured values.
    try:
        config = validate_steady_state_server_config(KworkConfig())
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
        sys.stderr.write("Сначала выполните kwork-mcp-bootstrap; справка: docs/configuration.md.\n")
        raise SystemExit(2) from None
    except ValueError as exc:
        sys.stderr.write(f"kwork-mcp: некорректная конфигурация: {exc}.\n")
        raise SystemExit(2) from None
    server = create_server(config=config)
    # The banner also triggers FastMCP's PyPI update check: an unproxied
    # network call and a cache file outside KWORK_STATE_DIR on every start.
    server.run(transport="stdio", show_banner=False)
