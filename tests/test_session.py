from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kwork_mcp.config import KworkConfig
from kwork_mcp.session import KworkSessionManager


def _make_config(**overrides) -> KworkConfig:
    defaults = {
        "login": "testuser",
        "password": "testpass",
        "token": None,
        "token_file": Path("/tmp/test_kwork_token"),
        "rps_limit": 100,
        "burst_limit": 100,
        "proxy_url": None,
        "timeout": 5,
        "phone_last": None,
    }
    defaults.update(overrides)
    with patch.dict("os.environ", {}, clear=False):
        return KworkConfig(**defaults)


@pytest.mark.asyncio
async def test_ensure_client_with_token(tmp_path: Path) -> None:
    cfg = _make_config(token="mytoken", token_file=tmp_path / "token")
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_kwork_cls.return_value = mock_client
        client = await session.ensure_client()
        assert client._token == "mytoken"
    await session.close()


@pytest.mark.asyncio
async def test_ensure_client_from_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("filetoken123")
    cfg = _make_config(token=None, token_file=token_file)
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_kwork_cls.return_value = mock_client
        client = await session.ensure_client()
        assert client._token == "filetoken123"
    await session.close()


@pytest.mark.asyncio
async def test_ensure_client_fresh_login(tmp_path: Path) -> None:
    cfg = _make_config(
        token=None,
        token_file=tmp_path / "nonexistent",
    )
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_client.get_token = AsyncMock(return_value="newtoken")
        mock_kwork_cls.return_value = mock_client
        await session.ensure_client()
        mock_client.get_token.assert_awaited_once()
    await session.close()


@pytest.mark.asyncio
async def test_ensure_client_no_credentials(tmp_path: Path) -> None:
    cfg = _make_config(
        login="",
        password="",
        token=None,
        token_file=tmp_path / "nonexistent",
    )
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork"), pytest.raises(RuntimeError, match="Авторизация не настроена"):
        await session.ensure_client()


@pytest.mark.asyncio
async def test_close_cleans_up() -> None:
    cfg = _make_config(token="tok")
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_kwork_cls.return_value = mock_client
        await session.ensure_client()
        await session.close()
        mock_client.close.assert_awaited_once()
        assert session._client is None
        assert session._web_logged_in is False
