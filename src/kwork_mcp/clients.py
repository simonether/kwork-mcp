"""Find Claude Desktop and Cursor configs and add the kwork-mcp server to them.

`kwork-mcp login` offers this so nobody has to edit JSON by hand. A config is
touched only after the user agrees, only if it parses as plain JSON, and the
previous version is kept next to it as `<name>.bak`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kwork_mcp import private_fs


class ClientConfigError(Exception):
    """The client config cannot be read as plain JSON; it was not changed."""


@dataclass(frozen=True, slots=True)
class ClientConfig:
    name: str
    path: Path


@dataclass(frozen=True, slots=True)
class Registration:
    outcome: Literal["added", "updated", "unchanged"]
    backup: Path | None


def _claude_desktop_dir() -> Path:
    if sys.platform == "win32":
        # The Microsoft Store (MSIX) build sees its own copy of AppData; a
        # config in the real AppData would be ignored.
        local = os.environ.get("LOCALAPPDATA")
        packages = (Path(local) if local else Path.home() / "AppData" / "Local") / "Packages"
        for package in sorted(packages.glob("Claude_*")):
            packaged = package / "LocalCache" / "Roaming" / "Claude"
            if packaged.is_dir():
                return packaged
        roaming = os.environ.get("APPDATA")
        directory = (Path(roaming) if roaming else Path.home() / "AppData" / "Roaming") / "Claude"
    elif sys.platform == "darwin":
        directory = Path.home() / "Library" / "Application Support" / "Claude"
    else:
        # No official Linux build; community builds follow XDG.
        xdg = os.environ.get("XDG_CONFIG_HOME")
        directory = (Path(xdg) if xdg else Path.home() / ".config") / "Claude"
    return directory


def find_clients() -> list[ClientConfig]:
    """The clients on this computer that have been started at least once."""

    found = []
    claude = _claude_desktop_dir()
    if claude.is_dir():
        found.append(ClientConfig("Claude Desktop", claude / "claude_desktop_config.json"))
    cursor = Path.home() / ".cursor"
    if cursor.is_dir():
        found.append(ClientConfig("Cursor", cursor / "mcp.json"))
    return found


def is_kwork_mcp_entry(entry: object) -> bool:
    """Whether a config entry starts kwork-mcp, in any version or from source."""

    if not isinstance(entry, dict):
        return False
    words = [entry.get("command"), *(entry.get("args") or [])]
    return any(
        isinstance(word, str) and (word == "kwork-mcp" or word.startswith(("kwork-mcp@", "kwork-mcp==")))
        for word in words
    )


def _read(path: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The config and its mcpServers, or None when the file does not exist."""

    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ClientConfigError(type(exc).__name__) from exc
    try:
        config = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise ClientConfigError("invalid_json") from exc
    if not isinstance(config, dict):
        raise ClientConfigError("not_an_object")
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClientConfigError("servers_not_an_object")
    return config, servers


def existing_entry(path: Path, name: str) -> object:
    """The entry `name` in the config, or None; raises ClientConfigError like `register_server`."""

    read = _read(path)
    return None if read is None else read[1].get(name)


def register_server(path: Path, name: str, entry: dict[str, Any]) -> Registration:
    """Set mcpServers[name] to `entry`, keeping everything else in the config."""

    read = _read(path)
    if read is None:
        servers: dict[str, Any] = {}
        config: dict[str, Any] = {"mcpServers": servers}
    else:
        config, servers = read
    previous = servers.get(name)
    if previous == entry:
        return Registration("unchanged", None)
    servers[name] = entry
    payload = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()

    backup = None
    if read is not None:
        backup = path.with_name(path.name + ".bak")
        # copy2 keeps the mode: the config may hold other servers' secrets.
        shutil.copy2(path, backup)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if read is not None:
            shutil.copymode(path, temp_name)
        private_fs.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return Registration("updated" if previous is not None else "added", backup)
