"""`kwork-mcp login` connects Claude Desktop and Cursor itself when the user agrees."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kwork_mcp import clients
from kwork_mcp.config import KworkConfig
from kwork_mcp.version import __version__
from tests.test_bootstrap_discovery import AuthClient, _environment, _run


@pytest.fixture
def claude_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "Claude"
    directory.mkdir()
    path = directory / "claude_desktop_config.json"
    monkeypatch.setattr(clients, "find_clients", lambda: [clients.ClientConfig("Claude Desktop", path)])
    monkeypatch.setattr("kwork_mcp.bootstrap.shutil.which", lambda name: f"/opt/tools/bin/{name}")
    _environment(monkeypatch, tmp_path / "state")
    return path


def _expected_entry(tmp_path: Path) -> dict[str, Any]:
    return {
        "command": "/opt/tools/bin/uvx",
        "args": [f"kwork-mcp@{__version__}"],
        "env": {"KWORK_STATE_DIR": str(tmp_path / "state")},
    }


def _login(tmp_path: Path, answers: str) -> tuple[int, str, str]:
    auth_clients: list[AuthClient] = []
    configs: list[KworkConfig] = []
    return asyncio.run(_run(tmp_path, confirmation=answers, clients=auth_clients, configs=configs))


def test_login_adds_the_server_to_claude_desktop_after_a_yes(tmp_path: Path, claude_config: Path) -> None:
    code, stdout, stderr = _login(tmp_path, "да\nда\n")

    assert code == 0
    assert "Подключить kwork-mcp к Claude Desktop? [y/N]" in stderr
    assert json.loads(claude_config.read_text(encoding="utf-8")) == {"mcpServers": {"kwork": _expected_entry(tmp_path)}}
    assert "Claude Desktop: kwork-mcp подключён." in stdout
    assert "Другие агенты. Claude Code:" in stdout
    # The manual block remains only for the client that was not set up.
    assert "Claude Desktop и Cursor" not in stdout
    assert 'Cursor: в "mcpServers" файла' in stdout


def test_login_keeps_other_servers_and_backs_the_config_up(tmp_path: Path, claude_config: Path) -> None:
    claude_config.write_text(json.dumps({"mcpServers": {"files": {"command": "npx", "args": ["fs"]}}}))

    code, stdout, _stderr = _login(tmp_path, "да\nда\n")

    assert code == 0
    servers = json.loads(claude_config.read_text(encoding="utf-8"))["mcpServers"]
    assert servers == {"files": {"command": "npx", "args": ["fs"]}, "kwork": _expected_entry(tmp_path)}
    assert str(claude_config.with_name("claude_desktop_config.json.bak")) in stdout


def test_login_leaves_the_config_alone_after_a_no(tmp_path: Path, claude_config: Path) -> None:
    code, stdout, _stderr = _login(tmp_path, "да\nнет\n")

    assert code == 0
    assert not claude_config.exists()
    assert 'Claude Desktop и Cursor: в "mcpServers" файла' in stdout


def test_login_asks_before_replacing_another_server_named_kwork(tmp_path: Path, claude_config: Path) -> None:
    other = {"mcpServers": {"kwork": {"command": "node", "args": ["/opt/other-kwork/index.js"]}}}
    claude_config.write_text(json.dumps(other))

    code, _stdout, stderr = _login(tmp_path, "да\nнет\n")

    assert code == 0
    assert "уже есть другой сервер «kwork»" in stderr
    assert json.loads(claude_config.read_text(encoding="utf-8")) == other


def test_login_never_edits_a_config_that_is_not_plain_json(tmp_path: Path, claude_config: Path) -> None:
    claude_config.write_text('{"mcpServers": {},}')

    code, stdout, stderr = _login(tmp_path, "да\n")

    assert code == 0
    assert "Подключить" not in stderr
    assert "не изменён" in stdout
    assert claude_config.read_text() == '{"mcpServers": {},}'
    assert 'Claude Desktop и Cursor: в "mcpServers" файла' in stdout


def test_login_does_not_ask_again_when_the_server_is_already_there(tmp_path: Path, claude_config: Path) -> None:
    claude_config.write_text(json.dumps({"mcpServers": {"kwork": _expected_entry(tmp_path)}}))

    code, stdout, stderr = _login(tmp_path, "да\n")

    assert code == 0
    assert "Подключить" not in stderr
    assert "Claude Desktop: kwork-mcp уже подключён." in stdout
