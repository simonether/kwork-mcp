"""`kwork-mcp login` and friends, and serving the single bound account by default."""

from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kwork.schema.actor import Actor

import kwork_mcp
from kwork_mcp.bootstrap import _connection_instructions, run_bootstrap_cli
from kwork_mcp.config import (
    AccountSelectionError,
    KworkConfig,
    bound_account_ids,
    load_server_config,
    select_bound_account,
)
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.security import SecureTokenStore, TokenRecord
from kwork_mcp.version import __version__


class TTYBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in tuple(os.environ):
        if name.upper().startswith("KWORK_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    monkeypatch.setenv("KWORK_STATE_DIR", str(path))
    return path


def _bind(state_dir: Path, *account_ids: int) -> None:
    tokens = state_dir / "tokens"
    tokens.mkdir(mode=0o700, exist_ok=True)
    for account_id in account_ids:
        (tokens / f"account-{account_id}.json").write_text("{}")
        (tokens / f"account-{account_id}.lock").write_text("")


# --- which accounts are bound


def test_bound_accounts_are_read_from_token_file_names(state_dir: Path) -> None:
    assert bound_account_ids(state_dir) == []
    _bind(state_dir, 77, 42)
    for stray in ("account-0.json", "account-42.json.tmp", "unbound-abc.json", "account-x.json"):
        (state_dir / "tokens" / stray).write_text("{}")

    assert bound_account_ids(state_dir) == [42, 77]


def test_a_single_bound_account_is_selected(state_dir: Path) -> None:
    _bind(state_dir, 42)

    assert select_bound_account(state_dir) == 42


def test_no_bound_account_points_to_login(state_dir: Path) -> None:
    with pytest.raises(AccountSelectionError) as refused:
        select_bound_account(state_dir)

    assert f"uvx kwork-mcp@{__version__} login" in str(refused.value)


def test_several_bound_accounts_need_an_explicit_choice(state_dir: Path) -> None:
    _bind(state_dir, 42, 77)

    with pytest.raises(AccountSelectionError) as refused:
        select_bound_account(state_dir)

    assert "42, 77" in str(refused.value)
    assert "KWORK_EXPECTED_USER_ID" in str(refused.value)


def test_unreadable_token_directory_is_reported_not_treated_as_empty(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(_self: Path) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", deny)

    with pytest.raises(AccountSelectionError, match="PermissionError"):
        bound_account_ids(state_dir)


# --- server configuration


def test_server_serves_the_single_bound_account_with_writes(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(state_dir, 42)
    monkeypatch.setenv("KWORK_WRITES", "auto")

    config = load_server_config()

    assert config.expected_user_id == 42
    assert config.writes == "auto"
    assert config.bootstrap_scope == "account-42"


def test_explicit_account_wins_over_the_bound_accounts(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(state_dir, 42, 77)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "77")

    assert load_server_config().expected_user_id == 77


def test_server_refuses_to_guess_between_bound_accounts(state_dir: Path) -> None:
    _bind(state_dir, 42, 77)

    with pytest.raises(AccountSelectionError):
        load_server_config()


def test_main_starts_the_server_for_the_bound_account(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind(state_dir, 42)
    configs: list[KworkConfig] = []

    def fake_create_server(*, config: KworkConfig) -> SimpleNamespace:
        configs.append(config)
        return SimpleNamespace(run=lambda **_kwargs: None)

    monkeypatch.setattr("kwork_mcp.server.create_server", fake_create_server)

    kwork_mcp.main([])

    assert configs[0].expected_user_id == 42


def test_main_explains_an_ambiguous_account_without_starting(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _bind(state_dir, 42, 77)
    monkeypatch.setattr(
        "kwork_mcp.server.create_server",
        lambda **_kwargs: pytest.fail("server must not start for an ambiguous account"),
    )

    with pytest.raises(SystemExit) as exited:
        kwork_mcp.main([])

    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert "несколько аккаунтов Kwork (42, 77)" in captured.err
    assert captured.out == ""


# --- terminal commands on the main entry point


def test_main_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    kwork_mcp.main(["--version"])

    assert capsys.readouterr().out == f"{__version__}\n"


def test_main_help_lists_the_terminal_commands(capsys: pytest.CaptureFixture[str]) -> None:
    kwork_mcp.main(["--help"])

    out = capsys.readouterr().out
    for command in ("kwork-mcp login", "kwork-mcp pending-writes", "kwork-mcp resolve-write"):
        assert command in out


def test_main_rejects_unknown_arguments_without_echoing_them(
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        kwork_mcp.main(["--token=argv-secret-sentinel"])

    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert "неизвестная команда" in captured.err
    assert "argv-secret-sentinel" not in captured.err + captured.out


def test_main_login_requires_a_terminal(
    state_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        kwork_mcp.main(["login"])

    assert exited.value.code == 2
    assert "TTY" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_login_takes_no_further_arguments(state_dir: Path) -> None:
    code = await run_bootstrap_cli(
        ["login", "user@example.com"],
        stdin=TTYBuffer(),
        stdout=(stdout := io.StringIO()),
        stderr=(stderr := TTYBuffer()),
    )

    assert code == 2
    assert "неизвестная команда" in stderr.getvalue()
    assert "user@example.com" not in stderr.getvalue() + stdout.getvalue()


@pytest.mark.asyncio
async def test_pending_writes_uses_the_single_bound_account(state_dir: Path) -> None:
    _bind(state_dir, 42)
    await CoordinationStore(KworkConfig(expected_user_id=42)).list_unresolved_writes("account-42")

    code = await run_bootstrap_cli(
        ["pending-writes"],
        stdin=TTYBuffer(),
        stdout=(stdout := io.StringIO()),
        stderr=TTYBuffer(),
    )

    assert code == 0
    assert '"account_id": 42' in stdout.getvalue()


@pytest.mark.asyncio
async def test_pending_writes_refuses_to_guess_between_accounts(state_dir: Path) -> None:
    _bind(state_dir, 42, 77)

    code = await run_bootstrap_cli(
        ["pending-writes"],
        stdin=TTYBuffer(),
        stdout=(stdout := io.StringIO()),
        stderr=(stderr := TTYBuffer()),
    )

    assert code == 2
    assert "KWORK_EXPECTED_USER_ID" in stderr.getvalue()
    assert stdout.getvalue() == ""


# --- what login prints at the end


def _instructions(**overrides: Any) -> str:
    config = KworkConfig(expected_user_id=42, **overrides)
    return _connection_instructions(Actor(id=42, username="found-user"), config, legacy_file_retained=False)


def test_login_prints_the_shortest_commands_for_the_default_setup(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default = Path(os.environ["HOME"]) / ".local" / "state" / "kwork-mcp"
    monkeypatch.setenv("KWORK_STATE_DIR", str(default))

    lines = _instructions().splitlines()

    assert lines[0] == "Аккаунт found-user (user_id 42) подключён."
    assert f"  claude mcp add kwork --scope user -- uvx kwork-mcp@{__version__}" in lines
    assert f"  codex mcp add kwork -- uvx kwork-mcp@{__version__}" in lines
    assert any("https://simonether.github.io/kwork-mcp/" in line for line in lines)


def test_login_names_the_account_when_several_are_bound(state_dir: Path) -> None:
    _bind(state_dir, 42, 77)

    output = _instructions()

    assert f"claude mcp add kwork --scope user -e KWORK_EXPECTED_USER_ID=42 -e KWORK_STATE_DIR={state_dir} --" in output
    assert f"codex mcp add kwork --env KWORK_EXPECTED_USER_ID=42 --env KWORK_STATE_DIR={state_dir} --" in output


def test_login_names_the_account_when_the_token_directory_is_unreadable(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(_self: Path) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", deny)

    assert "-e KWORK_EXPECTED_USER_ID=42" in _instructions()


def test_login_quotes_a_state_directory_with_spaces(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spaced = state_dir.parent / "my state"
    monkeypatch.setenv("KWORK_STATE_DIR", str(spaced))

    assert f"-e 'KWORK_STATE_DIR={spaced}' --" in _instructions()


# --- logout and status


def _store_login(state_dir: Path, account_id: int, username: str = "found-user", proxy: str | None = None) -> None:
    store = SecureTokenStore(state_dir)
    scope = f"account-{account_id}"
    lock = store.acquire_lock(scope)
    try:
        store.save_locked(
            scope,
            TokenRecord.create(user_id=account_id, username=username, token="stored-token-sentinel", proxy_url=proxy),
        )
    finally:
        store.release_lock(lock)


async def _logout(argv: list[str], answer: str = "да\n") -> tuple[int, str, str]:
    code = await run_bootstrap_cli(
        ["logout", *argv],
        stdin=TTYBuffer(answer),
        stdout=(stdout := io.StringIO()),
        stderr=(stderr := TTYBuffer()),
    )
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.asyncio
async def test_logout_forgets_the_single_stored_login_after_confirmation(state_dir: Path) -> None:
    _store_login(state_dir, 42)

    code, stdout, stderr = await _logout([])

    assert code == 0
    assert "found-user (user_id 42)" in stderr
    assert "удалён с этого компьютера" in stdout
    assert bound_account_ids(state_dir) == []
    assert "stored-token-sentinel" not in stdout + stderr


@pytest.mark.asyncio
async def test_logout_keeps_the_login_when_not_confirmed(state_dir: Path) -> None:
    _store_login(state_dir, 42)

    code, stdout, stderr = await _logout([], answer="нет\n")

    assert code == 1
    assert "не удалён" in stderr
    assert stdout == ""
    assert bound_account_ids(state_dir) == [42]


@pytest.mark.asyncio
async def test_logout_picks_one_of_several_accounts_by_id(state_dir: Path) -> None:
    _store_login(state_dir, 42)
    _store_login(state_dir, 77, username="second-user")

    refused, _stdout, stderr = await _logout([])
    assert refused == 2
    assert "42, 77" in stderr

    code, stdout, _stderr = await _logout(["77"])
    assert code == 0
    assert "second-user (user_id 77)" in stdout
    assert bound_account_ids(state_dir) == [42]


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [["not-an-id"], ["0"], ["42", "77"], ["--token=logout-secret"]])
async def test_logout_rejects_bad_arguments_without_echoing_them(state_dir: Path, argv: list[str]) -> None:
    _store_login(state_dir, 42)

    code, stdout, stderr = await _logout(argv)

    assert code == 2
    assert "Использование: kwork-mcp logout [user_id]" in stderr
    assert "logout-secret" not in stdout + stderr
    assert bound_account_ids(state_dir) == [42]


@pytest.mark.asyncio
async def test_logout_reports_a_missing_login(state_dir: Path) -> None:
    code, _stdout, stderr = await _logout(["42"])

    assert code == 1
    assert "для user_id 42 нет" in stderr


@pytest.mark.asyncio
async def test_logout_requires_a_terminal(state_dir: Path) -> None:
    _store_login(state_dir, 42)

    code = await run_bootstrap_cli(
        ["logout"], stdin=io.StringIO("да\n"), stdout=io.StringIO(), stderr=(stderr := io.StringIO())
    )

    assert code == 2
    assert "TTY" in stderr.getvalue()
    assert bound_account_ids(state_dir) == [42]


async def _status() -> tuple[int, str]:
    code = await run_bootstrap_cli(
        ["status"], stdin=io.StringIO(), stdout=(stdout := io.StringIO()), stderr=io.StringIO()
    )
    return code, stdout.getvalue()


@pytest.mark.asyncio
async def test_status_describes_the_account_the_server_would_serve(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_login(state_dir, 42, proxy="http://proxy-user:proxy-pass@proxy.example:8080")
    monkeypatch.setenv("KWORK_SITE", "com")
    monkeypatch.setenv("KWORK_WRITES", "auto")

    code, output = await _status()

    assert code == 0
    assert "Аккаунт: found-user (user_id 42), единственный сохранённый вход" in output
    assert "Сайт: kwork.com" in output
    assert "Отправка: агент отправляет сам" in output
    assert "Прокси: задан" in output
    assert "Несверенные отправки: нет" in output
    for secret in ("stored-token-sentinel", "proxy-pass", "proxy.example"):
        assert secret not in output


@pytest.mark.asyncio
async def test_status_names_an_explicit_account_and_reads_the_ledger(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store_login(state_dir, 42)
    _store_login(state_dir, 77)
    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "77")
    await CoordinationStore(KworkConfig(expected_user_id=77)).list_unresolved_writes("account-77")

    code, output = await _status()

    assert code == 0
    assert "(user_id 77), из KWORK_EXPECTED_USER_ID" in output
    assert "Отправка: с подтверждением каждой" in output
    assert "Несверенные отправки: нет" in output


@pytest.mark.asyncio
async def test_status_explains_why_the_server_would_not_start(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, output = await _status()
    assert code == 2
    assert "не запустится. Аккаунт Kwork не подключён" in output

    _store_login(state_dir, 42)
    _store_login(state_dir, 77)
    code, output = await _status()
    assert code == 2
    assert "kwork-mcp logout <user_id>" in output

    monkeypatch.setenv("KWORK_EXPECTED_USER_ID", "99")
    code, output = await _status()
    assert code == 2
    assert "Для user_id 99 нет сохранённого входа" in output

    monkeypatch.setenv("KWORK_PASSWORD", "status-secret-sentinel")
    code, output = await _status()
    assert code == 2
    assert "В окружении есть логин" in output
    assert "status-secret-sentinel" not in output


@pytest.mark.asyncio
async def test_status_takes_no_arguments(state_dir: Path) -> None:
    code = await run_bootstrap_cli(
        ["status", "extra"], stdin=io.StringIO(), stdout=io.StringIO(), stderr=(stderr := io.StringIO())
    )

    assert code == 2
    assert "status не принимает аргументы" in stderr.getvalue()


def test_login_prints_a_claude_desktop_entry_with_the_absolute_uvx_path(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kwork_mcp.bootstrap.shutil.which", lambda name: f"/opt/tools/bin/{name}")
    monkeypatch.setenv("KWORK_SITE", "com")

    output = _instructions()

    assert (
        f'"kwork-com": {{"command": "/opt/tools/bin/uvx", "args": ["kwork-mcp@{__version__}"], '
        f'"env": {{"KWORK_SITE": "com", "KWORK_STATE_DIR": "{state_dir}"}}}}'
    ) in output


def test_login_falls_back_to_plain_uvx_when_it_is_not_on_path(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kwork_mcp.bootstrap.shutil.which", lambda _name: None)
    default = Path(os.environ["HOME"]) / ".local" / "state" / "kwork-mcp"
    monkeypatch.setenv("KWORK_STATE_DIR", str(default))

    assert f'"kwork": {{"command": "uvx", "args": ["kwork-mcp@{__version__}"]}}' in _instructions()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("KWORK_STATE_DIR", "relative/state", "Некорректная конфигурация: KWORK_STATE_DIR"),
        ("KWORK_PERSIST_TOKEN", "false", "KWORK_PERSIST_TOKEN=true"),
    ],
)
async def test_status_reports_invalid_configuration(
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    expected: str,
) -> None:
    _store_login(state_dir, 42)
    monkeypatch.setenv(name, value)

    code, output = await _status()

    assert code == 2
    assert expected in output
