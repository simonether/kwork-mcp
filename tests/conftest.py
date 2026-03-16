from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kwork_mcp.config import KworkConfig
from kwork_mcp.session import KworkSessionManager, _set_client_token


@pytest.fixture
def mock_config(tmp_path: Path) -> KworkConfig:
    """KworkConfig with test values and fast rate limiter."""
    with patch.dict("os.environ", {}, clear=False):
        return KworkConfig(
            login="testuser",
            password="testpass",
            token=None,
            token_file=tmp_path / "test_kwork_token",
            rps_limit=100,
            burst_limit=100,
            proxy_url=None,
            timeout=5,
            phone_last=None,
        )


@pytest.fixture
def mock_client() -> AsyncMock:
    """AsyncMock for a Kwork client."""
    client = AsyncMock()
    _set_client_token(client, "")
    return client


@pytest.fixture
def mock_session(mock_config: KworkConfig, mock_client: AsyncMock) -> KworkSessionManager:
    """KworkSessionManager with a pre-attached mock client."""
    session = KworkSessionManager(mock_config)
    session._client = mock_client
    return session
