from __future__ import annotations

import base64
import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote

from kwork_mcp.config import SERVER_SECRET_ENV_NAMES
from kwork_mcp.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_registry_metadata_advertises_only_safe_server_environment() -> None:
    metadata = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    package = metadata["packages"][0]
    variables = package["environmentVariables"]
    by_name = {item["name"]: item for item in variables}

    assert len(variables) == len(by_name) == 25
    assert set(by_name).isdisjoint(SERVER_SECRET_ENV_NAMES)
    assert all(item["isSecret"] is False for item in variables)
    # `kwork-mcp login` binds the account; the ID is needed only to pick one of several.
    assert by_name["KWORK_EXPECTED_USER_ID"]["isRequired"] is False
    assert not any(item["isRequired"] for item in variables)
    assert by_name["KWORK_PERSIST_TOKEN"]["default"] == "true"
    assert by_name["KWORK_AUTH_LOCK_TIMEOUT"]["default"] == "90"
    assert by_name["KWORK_SITE"]["default"] == "ru"
    assert by_name["KWORK_SITE"]["choices"] == ["ru", "com"]


def test_example_environment_has_no_active_secret_assignment() -> None:
    active_names = {
        line.partition("=")[0].strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert active_names.isdisjoint(SERVER_SECRET_ENV_NAMES)
    assert {"KWORK_PERSIST_TOKEN", "KWORK_WRITES"} <= active_names


def test_versions_and_console_entrypoints_are_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert project["dynamic"] == ["version"]
    assert registry["version"] == __version__ == "1.6.0"
    assert registry["packages"][0]["version"] == __version__
    assert project["scripts"] == {
        "kwork-mcp": "kwork_mcp:main",
        "kwork-mcp-bootstrap": "kwork_mcp.bootstrap:main",
    }


def test_install_instructions_pin_the_current_version() -> None:
    for name in ("README.md", "site/index.html"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert set(re.findall(r"kwork-mcp(?:==|@)([0-9][0-9A-Za-z.]*)", text)) == {__version__}, name
        # The "Add to Cursor" link carries its own base64 copy of the config.
        configs = re.findall(r"install-mcp\?name=kwork&(?:amp;)?config=([A-Za-z0-9+/%=]+)", text)
        assert configs, name
        for encoded in configs:
            config = json.loads(base64.b64decode(unquote(encoded)))
            assert config == {"command": "uvx", "args": [f"kwork-mcp@{__version__}"]}, name

    site = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    assert f'"softwareVersion": "{__version__}"' in site


def test_release_workflow_fails_closed_for_prerelease_registry_publish() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "source_is_prerelease = Version(__version__).is_prerelease" in workflow
    assert "source_is_prerelease != event_is_prerelease" in workflow
    assert "is_prerelease: ${{ steps.release_metadata.outputs.is_prerelease }}" in workflow
    assert "needs: [package, publish-pypi]" in workflow
    assert "needs.package.outputs.is_prerelease == 'false'" in workflow
    assert "!github.event.release.prerelease" in workflow


def test_workflows_use_explicit_locked_mode_without_conflicting_environment() -> None:
    for name in ("ci.yml", "release.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "UV_FROZEN" not in workflow
        assert "uv sync --locked --all-groups" in workflow


def test_release_asset_upload_does_not_require_a_checkout() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'gh release upload "$RELEASE_TAG" dist/* --repo "$GITHUB_REPOSITORY"' in workflow
