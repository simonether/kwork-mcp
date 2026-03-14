from __future__ import annotations

from pathlib import Path

import pytest


def test_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KWORK_LOGIN", raising=False)
    monkeypatch.delenv("KWORK_PASSWORD", raising=False)
    monkeypatch.delenv("KWORK_TOKEN", raising=False)
    monkeypatch.setenv("KWORK_LOGIN", "testuser")
    monkeypatch.setenv("KWORK_PASSWORD", "testpass")

    from kwork_mcp.config import KworkConfig

    cfg = KworkConfig()
    assert cfg.login == "testuser"
    assert cfg.password.get_secret_value() == "testpass"
    assert cfg.rps_limit == 2
    assert cfg.burst_limit == 5
    assert cfg.proxy_url is None
    assert cfg.token is None
    assert cfg.token_file == Path.home() / ".kwork_token"


def test_config_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KWORK_TOKEN", "mytoken123")
    monkeypatch.setenv("KWORK_LOGIN", "")
    monkeypatch.setenv("KWORK_PASSWORD", "")

    from kwork_mcp.config import KworkConfig

    cfg = KworkConfig()
    assert cfg.token is not None
    assert cfg.token.get_secret_value() == "mytoken123"
