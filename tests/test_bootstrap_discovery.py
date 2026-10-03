"""Bootstrap without a preset account ID: discover, confirm, then bind."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from kwork.schema.actor import Actor

from kwork_mcp.bootstrap import run_bootstrap_cli
from kwork_mcp.config import KworkConfig
from kwork_mcp.security import SecureTokenStore


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class AuthClient:
    def __init__(self, actor: Actor) -> None:
        self.actor = actor
        self._token: str | None = None
        self.get_token_calls = 0
        self.get_me_calls = 0
        self.closed = 0

    async def get_token(self) -> str:
        self.get_token_calls += 1
        return "discovered-token-value"

    async def get_me(self) -> Actor:
        self.get_me_calls += 1
        return self.actor

    async def close(self) -> None:
        self.closed += 1


def _environment(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    for name in tuple(os.environ):
        if name.upper().startswith("KWORK_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KWORK_STATE_DIR", str(state_dir))


async def _run(
    tmp_path: Path,
    *,
    confirmation: str,
    clients: list[AuthClient],
    configs: list[KworkConfig],
) -> tuple[int, str, str]:
    answers = iter(("user@example.com", "password-value", "", ""))

    def factory(config: KworkConfig) -> AuthClient:
        configs.append(config)
        client = AuthClient(Actor(id=4242, username="found-user"))
        clients.append(client)
        return client

    stdout = io.StringIO()
    stderr = TTYBuffer()
    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer(confirmation),
        stdout=stdout,
        stderr=stderr,
        getpass_fn=lambda *_args, **_kwargs: next(answers),
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=tmp_path / "home",
    )
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.asyncio
async def test_bootstrap_without_account_id_binds_the_confirmed_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    _environment(monkeypatch, state_dir)
    clients: list[AuthClient] = []
    configs: list[KworkConfig] = []

    code, stdout, stderr = await _run(tmp_path, confirmation="да\n", clients=clients, configs=configs)

    assert code == 0
    assert "found-user" in stderr
    assert "4242" in stderr
    assert stdout.startswith("Аккаунт found-user (user_id 4242) подключён.")
    # The only bound account needs no KWORK_EXPECTED_USER_ID in the client config.
    assert "KWORK_EXPECTED_USER_ID" not in stdout
    # One real sign-in; the bind step reuses the token it produced.
    assert sum(client.get_token_calls for client in clients) == 1
    assert configs[-1].expected_user_id == 4242
    assert configs[-1].login == ""
    store = SecureTokenStore(state_dir)
    lock = store.acquire_lock("account-4242")
    try:
        record = store.load_locked("account-4242")
    finally:
        store.release_lock(lock)
    assert record is not None
    assert record.user_id == 4242
    assert record.username == "found-user"
    assert "discovered-token-value" not in stdout + stderr
    assert all(client.closed == 1 for client in clients)


@pytest.mark.asyncio
async def test_bootstrap_without_account_id_stores_nothing_when_declined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    _environment(monkeypatch, state_dir)
    clients: list[AuthClient] = []

    code, stdout, stderr = await _run(tmp_path, confirmation="нет\n", clients=clients, configs=[])

    assert code == 1
    assert stdout == ""
    assert "не изменён" in stderr
    assert not (state_dir / "tokens" / "account-4242.json").exists()
    assert all(client.closed == 1 for client in clients)


@pytest.mark.asyncio
async def test_legacy_token_import_without_account_id_discovers_without_signing_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    _environment(monkeypatch, state_dir)
    home = tmp_path / "home"
    home.mkdir()
    legacy = home / ".kwork_token"
    legacy.write_text("legacy-token-value\n")
    os.chmod(legacy, 0o600)
    clients: list[AuthClient] = []

    def factory(config: KworkConfig) -> AuthClient:
        client = AuthClient(Actor(id=4242, username="found-user"))
        clients.append(client)
        return client

    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer("y\nда\n"),
        stdout=(stdout := io.StringIO()),
        stderr=TTYBuffer(),
        getpass_fn=lambda *_args, **_kwargs: "",
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=home,
    )

    assert code == 0
    assert stdout.getvalue().startswith("Аккаунт found-user (user_id 4242) подключён.")
    assert sum(client.get_token_calls for client in clients) == 0


class FailingProfileClient(AuthClient):
    async def get_me(self) -> Actor:
        raise ConnectionResetError("upstream closed")


@pytest.mark.asyncio
async def test_discovery_failure_stores_nothing_and_closes_the_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "state"
    _environment(monkeypatch, state_dir)
    answers = iter(("user@example.com", "password-value", "", ""))
    clients: list[AuthClient] = []

    def factory(config: KworkConfig) -> AuthClient:
        client = FailingProfileClient(Actor(id=4242, username="found-user"))
        clients.append(client)
        return client

    code = await run_bootstrap_cli(
        [],
        stdin=TTYBuffer("да\n"),
        stdout=(stdout := io.StringIO()),
        stderr=(stderr := TTYBuffer()),
        getpass_fn=lambda *_args, **_kwargs: next(answers),
        client_factory=factory,  # type: ignore[arg-type]
        home_dir=tmp_path / "home",
    )

    assert code == 1
    assert stdout.getvalue() == ""
    assert "upstream_unavailable" in stderr.getvalue()
    assert not (state_dir / "tokens" / "account-4242.json").exists()
    assert [client.closed for client in clients] == [1]
