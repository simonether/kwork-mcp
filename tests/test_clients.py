"""Finding Claude Desktop and Cursor configs and adding kwork-mcp to them safely."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from kwork_mcp import clients
from kwork_mcp.clients import ClientConfigError, find_clients, is_kwork_mcp_entry, register_server
from tests.platforms import posix_only

ENTRY = {"command": "/opt/tools/bin/uvx", "args": ["kwork-mcp@1.6.0"]}

# --- where the configs live


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))
    for name in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    return path


def _names(found: list[clients.ClientConfig]) -> dict[str, Path]:
    return {client.name: client.path for client in found}


def test_nothing_is_offered_when_no_client_was_ever_started(home: Path) -> None:
    assert find_clients() == []


def test_macos_claude_desktop_and_cursor(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    (home / "Library" / "Application Support" / "Claude").mkdir(parents=True)
    (home / ".cursor").mkdir()

    assert _names(find_clients()) == {
        "Claude Desktop": home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        "Cursor": home / ".cursor" / "mcp.json",
    }


def test_windows_claude_desktop_in_roaming_app_data(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    (home / "AppData" / "Roaming" / "Claude").mkdir(parents=True)

    assert _names(find_clients()) == {
        "Claude Desktop": home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
    }


def test_windows_store_claude_desktop_keeps_its_own_config(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The Microsoft Store (MSIX) build sees a private copy of AppData, so a
    # config written to the real one would be ignored.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    (home / "AppData" / "Roaming" / "Claude").mkdir(parents=True)
    packaged = home / "AppData" / "Local" / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
    packaged.mkdir(parents=True)

    assert _names(find_clients()) == {"Claude Desktop": packaged / "claude_desktop_config.json"}


def test_linux_community_claude_desktop_follows_xdg(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    (home / "config" / "Claude").mkdir(parents=True)
    (home / ".cursor").mkdir()

    assert _names(find_clients()) == {
        "Claude Desktop": home / "config" / "Claude" / "claude_desktop_config.json",
        "Cursor": home / ".cursor" / "mcp.json",
    }


# --- writing the entry


def test_a_missing_config_is_created_with_the_server(tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"

    result = register_server(path, "kwork", ENTRY)

    assert result.outcome == "added"
    assert result.backup is None
    assert json.loads(path.read_text(encoding="utf-8")) == {"mcpServers": {"kwork": ENTRY}}


def test_other_servers_and_settings_are_kept_and_the_old_file_is_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    original = {"globalShortcut": "Ctrl+Space", "mcpServers": {"files": {"command": "npx", "args": ["fs"]}}}
    path.write_text(json.dumps(original), encoding="utf-8")

    result = register_server(path, "kwork", ENTRY)

    assert result.outcome == "added"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "globalShortcut": "Ctrl+Space",
        "mcpServers": {"files": {"command": "npx", "args": ["fs"]}, "kwork": ENTRY},
    }
    assert result.backup == tmp_path / "claude_desktop_config.json.bak"
    assert json.loads(result.backup.read_text(encoding="utf-8")) == original


def test_an_older_kwork_mcp_entry_is_updated(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"kwork": {"command": "uvx", "args": ["kwork-mcp@1.5.2"]}}}))

    assert register_server(path, "kwork", ENTRY).outcome == "updated"
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["kwork"] == ENTRY


def test_the_same_entry_is_left_untouched(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"kwork": ENTRY}}))
    before = path.read_bytes()

    result = register_server(path, "kwork", ENTRY)

    assert result.outcome == "unchanged"
    assert path.read_bytes() == before
    assert not (tmp_path / "mcp.json.bak").exists()


def test_a_byte_order_mark_from_notepad_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {}}).encode())

    assert register_server(path, "kwork", ENTRY).outcome == "added"
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    "content",
    [
        '{"mcpServers": {"files": {}},}',
        "// my servers\n{}",
        "[]",
        '{"mcpServers": []}',
        b"\xff\xfe".decode("latin-1"),
    ],
    ids=["trailing-comma", "comment", "array", "servers-not-object", "not-utf8"],
)
def test_a_config_that_is_not_plain_json_is_never_touched(tmp_path: Path, content: str) -> None:
    path = tmp_path / "claude_desktop_config.json"
    path.write_bytes(content.encode("latin-1"))
    before = path.read_bytes()

    with pytest.raises(ClientConfigError):
        register_server(path, "kwork", ENTRY)

    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["claude_desktop_config.json"]


@posix_only
def test_the_config_keeps_its_permissions(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text("{}")
    os.chmod(path, 0o600)

    register_server(path, "kwork", ENTRY)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "mcp.json.bak").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("entry", "ours"),
    [
        (ENTRY, True),
        ({"command": "uvx", "args": ["kwork-mcp@1.5.2"]}, True),
        ({"command": "uv", "args": ["--directory", "/src/kwork-mcp", "run", "kwork-mcp"]}, True),
        ({"command": "node", "args": ["/opt/other-kwork/index.js"]}, False),
        ({"command": "uvx", "args": ["kwork-mcp-fork"]}, False),
        ("not an object", False),
    ],
)
def test_only_kwork_mcp_entries_count_as_ours(entry: object, ours: bool) -> None:
    assert is_kwork_mcp_entry(entry) is ours
