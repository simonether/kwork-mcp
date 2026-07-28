from __future__ import annotations

import json
import tomllib
from pathlib import Path

from kwork_mcp.config import SERVER_SECRET_ENV_NAMES
from kwork_mcp.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_registry_metadata_advertises_only_safe_server_environment() -> None:
    metadata = json.loads((ROOT / "server.json").read_text())
    package = metadata["packages"][0]
    variables = package["environmentVariables"]
    by_name = {item["name"]: item for item in variables}

    assert len(variables) == len(by_name) == 24
    assert set(by_name).isdisjoint(SERVER_SECRET_ENV_NAMES)
    assert all(item["isSecret"] is False for item in variables)
    assert by_name["KWORK_EXPECTED_USER_ID"]["isRequired"] is True
    assert by_name["KWORK_PERSIST_TOKEN"]["default"] == "true"
    assert by_name["KWORK_AUTH_LOCK_TIMEOUT"]["default"] == "90"


def test_example_environment_has_no_active_secret_assignment() -> None:
    active_names = {
        line.partition("=")[0].strip()
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }
    assert active_names.isdisjoint(SERVER_SECRET_ENV_NAMES)
    assert {
        "KWORK_EXPECTED_USER_ID",
        "KWORK_PERSIST_TOKEN",
        "KWORK_ENABLE_WRITES",
    } <= active_names


def test_versions_and_console_entrypoints_are_consistent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    registry = json.loads((ROOT / "server.json").read_text())

    assert project["dynamic"] == ["version"]
    assert registry["version"] == __version__ == "1.0.0rc1"
    assert registry["packages"][0]["version"] == __version__
    assert project["scripts"] == {
        "kwork-mcp": "kwork_mcp:main",
        "kwork-mcp-bootstrap": "kwork_mcp.bootstrap:main",
    }


def test_release_workflow_fails_closed_for_prerelease_registry_publish() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "source_is_prerelease = Version(__version__).is_prerelease" in workflow
    assert "source_is_prerelease != event_is_prerelease" in workflow
    assert "is_prerelease: ${{ steps.release_metadata.outputs.is_prerelease }}" in workflow
    assert "needs: [package, publish-pypi]" in workflow
    assert "needs.package.outputs.is_prerelease == 'false'" in workflow
    assert "!github.event.release.prerelease" in workflow


def test_workflows_use_explicit_locked_mode_without_conflicting_environment() -> None:
    for name in ("ci.yml", "release.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text()
        assert "UV_FROZEN" not in workflow
        assert "uv sync --locked --all-groups" in workflow


def test_release_asset_upload_does_not_require_a_checkout() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert 'gh release upload "$RELEASE_TAG" dist/* --repo "$GITHUB_REPOSITORY"' in workflow
