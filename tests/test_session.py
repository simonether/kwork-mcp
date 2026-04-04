from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kwork_mcp.config import KworkConfig
from kwork_mcp.session import KworkSessionManager, _get_client_token


@pytest.mark.asyncio
async def test_ensure_client_with_token(tmp_path: Path, mock_config: KworkConfig) -> None:
    cfg = KworkConfig(
        **{**mock_config.model_dump(), "token": "mytoken", "token_file": tmp_path / "token"},
    )
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_kwork_cls.return_value = mock_client
        client = await session.ensure_client()
        assert _get_client_token(client) == "mytoken"
    await session.close()


@pytest.mark.asyncio
async def test_ensure_client_from_file(tmp_path: Path, mock_config: KworkConfig) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("filetoken123")
    cfg = KworkConfig(
        **{**mock_config.model_dump(), "token": None, "token_file": token_file},
    )
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_kwork_cls.return_value = mock_client
        client = await session.ensure_client()
        assert _get_client_token(client) == "filetoken123"
    await session.close()


@pytest.mark.asyncio
async def test_ensure_client_fresh_login(mock_config: KworkConfig) -> None:
    session = KworkSessionManager(mock_config)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_client.get_token = AsyncMock(return_value="newtoken")
        mock_kwork_cls.return_value = mock_client
        await session.ensure_client()
        mock_client.get_token.assert_awaited_once()
    await session.close()


@pytest.mark.asyncio
async def test_ensure_client_no_credentials(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=False):
        cfg = KworkConfig(
            login="",
            password="",
            token=None,
            token_file=tmp_path / "nonexistent",
            rps_limit=100,
            burst_limit=100,
            proxy_url=None,
            timeout=5,
            phone_last=None,
        )
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork"), pytest.raises(RuntimeError, match="Авторизация не настроена"):
        await session.ensure_client()


@pytest.mark.asyncio
async def test_make_client_passes_proxy_and_timeout(tmp_path: Path) -> None:
    with patch.dict("os.environ", {}, clear=False):
        cfg = KworkConfig(
            login="testuser",
            password="testpass",
            token="tok",
            token_file=tmp_path / "token",
            rps_limit=100,
            burst_limit=100,
            proxy_url="socks5://proxy.example.com:1080",
            timeout=15,
            phone_last=None,
        )
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_kwork_cls.return_value = AsyncMock()
        await session.ensure_client()
        mock_kwork_cls.assert_called_once_with(
            login="testuser",
            password="testpass",
            proxy="socks5://proxy.example.com:1080",
            phone_last=None,
            timeout=15,
        )
    await session.close()


@pytest.mark.asyncio
async def test_close_cleans_up(mock_config: KworkConfig) -> None:
    cfg = KworkConfig(**{**mock_config.model_dump(), "token": "tok"})
    session = KworkSessionManager(cfg)
    with patch("kwork_mcp.session.Kwork") as mock_kwork_cls:
        mock_client = AsyncMock()
        mock_kwork_cls.return_value = mock_client
        await session.ensure_client()
        await session.close()
        mock_client.close.assert_awaited_once()
        assert session._client is None
        assert session._web_logged_in is False
