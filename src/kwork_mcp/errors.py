from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError
from kwork.exceptions import KworkException, KworkHTTPException
from loguru import logger

if TYPE_CHECKING:
    from kwork_mcp.session import KworkSessionManager


def _get_error_code(exc: KworkException) -> int | None:
    """Extract Kwork API error_code from KworkHTTPException.response_json."""
    if isinstance(exc, KworkHTTPException) and exc.response_json:
        return exc.response_json.get("error_code")
    return None


@asynccontextmanager
async def api_guard(
    operation: str = "Kwork API",
    *,
    session: KworkSessionManager | None = None,
) -> AsyncIterator[None]:
    """Context manager that translates pykwork exceptions into ToolError."""
    try:
        yield
    except ToolError:
        raise
    except KworkHTTPException as exc:
        code = _get_error_code(exc)
        if code == 118 or "captcha" in str(exc).lower():
            raise ToolError(
                "Kwork требует капчу. Откройте kwork.ru в браузере, "
                "решите капчу, затем передайте свежий токен через KWORK_TOKEN."
            ) from exc
        if exc.status == 401:
            if session is not None:
                try:
                    await session.relogin()
                    raise ToolError("Сессия обновлена. Повторите запрос.") from exc
                except ToolError:
                    raise
                except Exception:
                    logger.exception("Relogin failed after 401")
            raise ToolError(
                "Сессия истекла, повторная авторизация не удалась. Проверьте KWORK_LOGIN и KWORK_PASSWORD."
            ) from exc
        if exc.status == 403 or code == 403:
            raise ToolError(
                "IP заблокирован Kwork. Попробуйте позже или используйте прокси (KWORK_PROXY_URL)."
            ) from exc
        logger.error("{op} HTTP error: status={status} {exc}", op=operation, status=exc.status, exc=exc)
        raise ToolError(f"Ошибка {operation} (HTTP {exc.status}). Подробности в логах.") from exc
    except KworkException as exc:
        logger.error("{op} error: {exc}", op=operation, exc=exc)
        raise ToolError(f"Ошибка {operation}. Подробности в логах.") from exc
    except TimeoutError as exc:
        raise ToolError("Kwork API недоступен (таймаут). Попробуйте позже.") from exc
    except Exception as exc:
        logger.exception("Unexpected error in {op}", op=operation)
        raise ToolError(f"Непредвиденная ошибка {operation}. Подробности в логах.") from exc
