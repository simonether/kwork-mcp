from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from kwork_mcp.config import KworkConfig
from kwork_mcp.errors import GatewayError
from kwork_mcp.security import (
    SecureTokenStore,
    TokenRecord,
    ensure_secure_directory,
    redact_text,
    sanitize_external,
)
from tests.platforms import assert_private_directory, assert_private_file, posix_only


def test_configuration_never_loads_dotenv_from_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("KWORK_TOKEN=stolen-from-cwd\n")
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith("KWORK_"):
            monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match="запуск без логина и пароля"):
        KworkConfig()


def test_configuration_defaults_to_confirmed_writes(tmp_path: Path) -> None:
    assert KworkConfig(token="x", state_dir=tmp_path).writes == "confirm"
    with pytest.raises(ValidationError, match="writes"):
        KworkConfig(token="x", state_dir=tmp_path, writes="maybe")
    config = KworkConfig(
        token="x",
        state_dir=tmp_path,
        writes="auto",
        expected_user_id=42,
        expected_username=" @Fixture ",
    )
    assert config.expected_username == "Fixture"
    assert config.bootstrap_scope == "account-42"
    assert "token=SecretStr('**********')" in repr(config)
    assert "token=SecretStr('x')" not in repr(config)


@pytest.mark.parametrize("proxy", ["file:///tmp/socket", "http:///missing-host", "bad\nurl"])
def test_proxy_validation_rejects_unsafe_values(tmp_path: Path, proxy: str) -> None:
    with pytest.raises(ValidationError):
        KworkConfig(token="x", state_dir=tmp_path, proxy_url=proxy)


def test_secret_inputs_are_hidden_in_validation_errors(tmp_path: Path) -> None:
    secret_proxy = "http://alice:proxy-password@proxy.example/private"
    with pytest.raises(ValidationError) as captured:
        KworkConfig(
            token="explicit-token-secret",
            state_dir=tmp_path,
            proxy_url=secret_proxy,
        )
    rendered = str(captured.value)
    assert secret_proxy not in rendered
    assert "proxy-password" not in rendered
    assert "explicit-token-secret" not in rendered
    assert "input_value=" not in rendered


@posix_only
def test_secure_directory_rejects_permissions_and_symlinks(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    os.chmod(unsafe, 0o755)
    with pytest.raises(GatewayError, match="Параметры"):
        ensure_secure_directory(unsafe)

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(GatewayError):
        ensure_secure_directory(link)


def test_token_store_roundtrip_is_private_atomic_and_account_scoped(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = SecureTokenStore(state_dir)
    record = TokenRecord.create(user_id=42, username="fixture", token="top-secret")
    fd = store.acquire_lock("account-42")
    try:
        store.save_locked("account-42", record)
        assert store.load_locked("account-42") == record
        token_path = state_dir / "tokens" / "account-42.json"
        assert_private_file(token_path)
        assert_private_directory(state_dir / "tokens")
        store.delete_locked("account-42")
        assert store.load_locked("account-42") is None
    finally:
        store.release_lock(fd)


@posix_only
def test_token_store_rejects_symlink_and_world_readable_token(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    store = SecureTokenStore(state_dir)
    tokens = state_dir / "tokens"
    outside = tmp_path / "outside"
    outside.write_text("{}")
    (tokens / "account-1.json").symlink_to(outside)
    with pytest.raises(GatewayError):
        store.load_locked("account-1")
    (tokens / "account-1.json").unlink()
    token = tokens / "account-1.json"
    token.write_text("{}")
    os.chmod(token, 0o644)
    with pytest.raises(GatewayError):
        store.load_locked("account-1")


def test_external_data_and_text_redaction() -> None:
    nested: object = {"password": "one", "safe": [{"authorization": "two"}]}
    assert sanitize_external(nested) == {
        "password": "<redacted>",
        "safe": [{"authorization": "<redacted>"}],
    }
    text = "proxy socks5://alice:pw@host token=abc"
    assert redact_text(text, ["abc"]) == "proxy socks5://<redacted>@host token=<redacted>"
