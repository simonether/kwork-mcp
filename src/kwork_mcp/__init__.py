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

    missing_binding = (
        "kwork-mcp: для запуска нужны KWORK_EXPECTED_USER_ID и KWORK_PERSIST_TOKEN=true; "
        "сначала выполните kwork-mcp-bootstrap.\n"
    )
    try:
        config = validate_steady_state_server_config(KworkConfig())
    except ValidationError as exc:
        fields = sorted({f"KWORK_{str(error['loc'][0]).upper()}" for error in exc.errors() if error.get("loc")})
        if not fields:
            sys.stderr.write(missing_binding)
        else:
            sys.stderr.write(
                "kwork-mcp: некорректная конфигурация: " + ", ".join(fields) + ". См. docs/configuration.md.\n"
            )
        raise SystemExit(2) from None
    except ValueError:
        sys.stderr.write(missing_binding)
        raise SystemExit(2) from None
    server = create_server(config=config)
    # The banner also triggers FastMCP's PyPI update check: an unproxied
    # network call and a cache file outside KWORK_STATE_DIR on every start.
    server.run(transport="stdio", show_banner=False)
