from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError
from kwork.exceptions import KworkException, KworkHTTPException

from kwork_mcp.errors import api_guard


def _make_http_exc(status: int, error_code: int | None = None) -> KworkHTTPException:
    exc = KworkHTTPException.__new__(KworkHTTPException)
    exc.status = status
    exc.response_json = {"error_code": error_code} if error_code is not None else None
    exc.args = (f"HTTP {status}",)
    return exc


@pytest.mark.asyncio
async def test_captcha_error() -> None:
    with pytest.raises(ToolError, match="капчу"):
        async with api_guard("test"):
            raise _make_http_exc(200, error_code=118)


@pytest.mark.asyncio
async def test_401_without_session() -> None:
    with pytest.raises(ToolError, match="Сессия истекла"):
        async with api_guard("test"):
            raise _make_http_exc(401)


@pytest.mark.asyncio
async def test_401_with_session_relogin_ok() -> None:
    session = MagicMock()
    session.relogin = AsyncMock()
    with pytest.raises(ToolError, match="Сессия обновлена"):
        async with api_guard("test", session=session):
            raise _make_http_exc(401)
    session.relogin.assert_awaited_once()


@pytest.mark.asyncio
async def test_401_with_session_relogin_fail() -> None:
    session = MagicMock()
    session.relogin = AsyncMock(side_effect=RuntimeError("no credentials"))
    with pytest.raises(ToolError, match="Сессия истекла"):
        async with api_guard("test", session=session):
            raise _make_http_exc(401)


@pytest.mark.asyncio
async def test_403_error() -> None:
    with pytest.raises(ToolError, match="IP заблокирован"):
        async with api_guard("test"):
            raise _make_http_exc(403)


@pytest.mark.asyncio
async def test_timeout_error() -> None:
    with pytest.raises(ToolError, match="таймаут"):
        async with api_guard("test"):
            raise TimeoutError("connection timed out")


@pytest.mark.asyncio
async def test_kwork_exception() -> None:
    with pytest.raises(ToolError, match="Подробности в логах"):
        async with api_guard("test"):
            raise KworkException("some error")


@pytest.mark.asyncio
async def test_generic_exception() -> None:
    with pytest.raises(ToolError, match=r"Непредвиденная ошибка.*Подробности в логах"):
        async with api_guard("test"):
            raise ValueError("unexpected")


@pytest.mark.asyncio
async def test_tool_error_passthrough() -> None:
    with pytest.raises(ToolError, match="custom error"):
        async with api_guard("test"):
            raise ToolError("custom error")
