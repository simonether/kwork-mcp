"""Windows support: owner and DACL checks, path syntax, locks, sealed tokens and client hints.

The pure pieces run everywhere; the Windows-only tests exercise the real
Win32 calls on the Windows CI job.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

import pytest
from kwork.schema.actor import Actor

import kwork_mcp
from kwork_mcp import private_fs
from kwork_mcp.bootstrap import _command_line, _connection_instructions, _warn_if_elevated, run_bootstrap_cli
from kwork_mcp.bootstrap import main as bootstrap_main
from kwork_mcp.config import KworkConfig, _default_state_dir, client_default_state_dir
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError
from kwork_mcp.models import ErrorCode
from kwork_mcp.security import SecureTokenStore, TokenRecord, ensure_secure_directory
from tests.platforms import posix_only, windows_only

USER = "S-1-5-21-1004336348-1177238915-682003330-1001"
OTHER_USER = "S-1-5-21-1004336348-1177238915-682003330-1002"
USERS = "S-1-5-32-545"
EVERYONE = "S-1-1-0"

# --- who may open a Windows object

FULL = 0x1F01FF
MODIFY = 0x1301BF
READ_EXECUTE = 0x1200A9
AUTHENTICATED_USERS = "S-1-5-11"


def allow(sid: str, mask: int = FULL, *, inherit_only: bool = False) -> tuple[str, str | None, int, bool]:
    return ("allow", sid, mask, inherit_only)


def test_owner_only_dacl_with_system_and_administrators_is_private() -> None:
    entries = [
        allow(USER),
        allow(private_fs.SYSTEM_SID),
        allow(private_fs.ADMINISTRATORS_SID),
        ("deny", None, 0, False),
    ]

    assert private_fs.windows_acl_problem(USER, entries, user_sid=USER) is None
    assert private_fs.windows_acl_problem(private_fs.ADMINISTRATORS_SID, entries, user_sid=USER) is None


@pytest.mark.parametrize("owner", [OTHER_USER, USERS, None])
def test_an_object_owned_by_someone_else_is_not_private(owner: str | None) -> None:
    assert private_fs.windows_acl_problem(owner, [allow(USER)], user_sid=USER) == "wrong_owner"


@pytest.mark.parametrize(
    "entries",
    [
        None,
        [allow(USER), allow(USERS, READ_EXECUTE)],
        [allow(EVERYONE)],
        [allow(OTHER_USER, 0x1)],
        [allow(USER), ("other", None, 0, False)],
    ],
    ids=["null-dacl", "users-read", "everyone-full", "other-user", "unknown-ace"],
)
def test_a_dacl_that_lets_anyone_else_in_is_not_private(entries: list[Any] | None) -> None:
    assert private_fs.windows_acl_problem(USER, entries, user_sid=USER) == "acl_not_private"


def test_owner_rights_count_as_the_trusted_owner() -> None:
    # os.mkdir(path, 0o700) on Windows grants SYSTEM, Administrators and OWNER RIGHTS.
    entries = [allow(private_fs.SYSTEM_SID), allow(private_fs.ADMINISTRATORS_SID), allow(private_fs.OWNER_RIGHTS_SID)]

    assert private_fs.windows_acl_problem(USER, entries, user_sid=USER) is None
    assert private_fs.windows_acl_problem(private_fs.OWNER_RIGHTS_SID, entries, user_sid=USER) == "wrong_owner"


def test_an_allow_entry_without_rights_grants_nothing() -> None:
    entries = [allow(USER), allow(EVERYONE, 0)]

    assert private_fs.windows_acl_problem(USER, entries, user_sid=USER) is None


def test_a_directory_must_not_pass_anyone_else_on_to_new_files() -> None:
    entries = [allow(USER), allow(USERS, READ_EXECUTE, inherit_only=True)]

    # A file ignores entries meant for what is created inside it later.
    assert private_fs.windows_acl_problem(USER, entries, user_sid=USER) is None
    assert private_fs.windows_acl_problem(USER, entries, user_sid=USER, directory=True) == "acl_not_private"


def test_a_directory_may_pass_rights_on_to_the_creator() -> None:
    entries = [allow(USER), allow(private_fs.CREATOR_OWNER_SID, 0x10000000, inherit_only=True)]

    assert private_fs.windows_acl_problem(USER, entries, user_sid=USER, directory=True) is None


# --- who may swap a directory above the state directory


def test_a_system_managed_ancestor_is_trusted() -> None:
    # C:\ on Windows 10/11: TrustedInstaller owns it; users may add folders and
    # pass Modify on to them, but may not rename or re-permission the root.
    entries = [
        allow(private_fs.ADMINISTRATORS_SID),
        allow(private_fs.SYSTEM_SID),
        allow(USERS, READ_EXECUTE),
        allow(AUTHENTICATED_USERS, MODIFY, inherit_only=True),
        allow(AUTHENTICATED_USERS, 0x4),
    ]

    problem = private_fs.windows_ancestor_problem(private_fs.TRUSTED_INSTALLER_SID, entries, user_sid=USER, root=True)

    assert problem is None


def test_an_ancestor_others_may_rename_is_rejected() -> None:
    # A folder made under a drive root inherits Authenticated Users: Modify.
    entries = [allow(USER), allow(AUTHENTICATED_USERS, MODIFY)]

    assert (
        private_fs.windows_ancestor_problem(USER, entries, user_sid=USER, root=False) == "untrusted_writable_ancestor"
    )
    # Modify on a drive root grants DELETE, which cannot rename a root.
    assert private_fs.windows_ancestor_problem(USER, entries, user_sid=USER, root=True) is None


@pytest.mark.parametrize(
    "entries",
    [None, [allow(EVERYONE, 0x40)], [allow(OTHER_USER, 0x40000)], [("other", None, 0, False)]],
    ids=["null-dacl", "delete-child", "write-dac", "unknown-ace"],
)
def test_an_ancestor_others_may_take_over_is_rejected(entries: list[Any] | None) -> None:
    problem = private_fs.windows_ancestor_problem(USER, entries, user_sid=USER, root=True)

    assert problem == "untrusted_writable_ancestor"


def test_an_ancestor_owned_by_someone_else_is_rejected() -> None:
    problem = private_fs.windows_ancestor_problem(OTHER_USER, [allow(USER)], user_sid=USER, root=False)

    assert problem == "untrusted_ancestor_owner"


# --- Windows path syntax that can name something else


@pytest.mark.parametrize(
    "raw",
    [
        r"C:\Users\Имя Фамилия\AppData\Local\kwork-mcp",
        r"D:\state\kwork.mcp",
    ],
)
def test_ordinary_windows_paths_are_accepted(raw: str) -> None:
    assert private_fs.windows_path_is_ambiguous(PureWindowsPath(raw)) is False


@pytest.mark.parametrize(
    "raw",
    [
        r"\\server\share\kwork",
        r"\\?\C:\kwork",
        r"\\.\C:\kwork",
        r"\kwork",
        r"C:kwork",
        r"C:\kwork:stream",
        r"C:\state\NUL",
        r"C:\state\con.txt",
        r"C:\state\COM1",
        r"C:\state\name.",
        r"C:\state\name ",
        r"C:\state\..\other",
    ],
)
def test_ambiguous_windows_paths_are_rejected(raw: str) -> None:
    assert private_fs.windows_path_is_ambiguous(PureWindowsPath(raw)) is True


# --- interprocess locks


def test_a_lock_is_exclusive_until_released(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    second = os.open(path, os.O_RDWR)
    try:
        private_fs.try_lock(first)
        with pytest.raises(BlockingIOError):
            private_fs.try_lock(second)
        private_fs.unlock(first)
        private_fs.try_lock(second)
        private_fs.unlock(second)
    finally:
        os.close(first)
        os.close(second)


def test_closing_the_file_releases_its_lock(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    first = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    private_fs.try_lock(first)
    os.close(first)

    second = os.open(path, os.O_RDWR)
    try:
        private_fs.try_lock(second)
        private_fs.unlock(second)
    finally:
        os.close(second)


# --- sealed secrets


def test_a_sealed_secret_opens_with_the_same_purpose() -> None:
    secret = b'{"token":"sealed-token-sentinel"}'

    sealed = private_fs.seal(secret, purpose="kwork-mcp token account-42")

    assert private_fs.unseal(sealed, purpose="kwork-mcp token account-42") == secret


@posix_only
def test_posix_relies_on_file_modes_and_stores_secrets_as_is() -> None:
    assert private_fs.seal(b"secret", purpose="any") == b"secret"


@windows_only
def test_windows_encrypts_a_sealed_secret_for_the_current_user_and_purpose() -> None:
    sealed = private_fs.seal(b"sealed-token-sentinel", purpose="kwork-mcp token account-42")

    assert b"sealed-token-sentinel" not in sealed
    with pytest.raises(OSError):
        private_fs.unseal(sealed, purpose="kwork-mcp token account-77")


# --- where the state lives


def test_windows_keeps_state_in_the_profile_not_in_app_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Windows shows packaged (MSIX) apps such as Claude Desktop their own copy
    # of AppData, so the server they start and the terminal would split state.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "profile"))

    assert _default_state_dir() == tmp_path / "profile" / ".local" / "state" / "kwork-mcp"
    assert client_default_state_dir() == _default_state_dir()


def test_posix_clients_start_the_server_without_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))

    assert client_default_state_dir() == tmp_path / "home" / ".local" / "state" / "kwork-mcp"


# --- what login prints on Windows


def test_windows_commands_are_quoted_for_powershell_and_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    command = _command_line(
        ["claude", "mcp", "add", "kwork", "-e", r"KWORK_STATE_DIR=C:\Users\Имя Фамилия\kwork", "--", "uvx", "kwork"]
    )

    assert command == r'claude mcp add kwork -e "KWORK_STATE_DIR=C:\Users\Имя Фамилия\kwork" -- uvx kwork'


def test_windows_login_names_the_windows_client_config_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in tuple(os.environ):
        if name.upper().startswith("KWORK_"):
            monkeypatch.delenv(name, raising=False)
    uvx = r"C:\Users\Имя Фамилия\.local\bin\uvx.exe"
    monkeypatch.setattr(shutil, "which", lambda _name: uvx)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "profile"))
    state_dir = tmp_path / "profile" / ".local" / "state" / "kwork-mcp"
    config = KworkConfig(expected_user_id=42, state_dir=state_dir)

    output = _connection_instructions(Actor(id=42, username="found-user"), config, legacy_file_retained=False)

    assert "KWORK_STATE_DIR" not in output
    assert json.dumps(uvx, ensure_ascii=False) in output
    # A packaged Claude Desktop keeps its config in its own AppData copy.
    assert "Settings → Developer → Edit Config" in output
    assert "%APPDATA%" not in output
    assert r"%USERPROFILE%\.cursor\mcp.json" in output
    assert "~/.cursor" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [[], ["logout"]])
async def test_windows_points_a_non_terminal_run_to_powershell(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    # Git Bash in its own mintty window hands programs pipes, not a console.
    monkeypatch.setattr(sys, "platform", "win32")
    stderr = io.StringIO()

    code = await run_bootstrap_cli(argv, stdin=io.StringIO(), stdout=io.StringIO(), stderr=stderr)

    assert code == 2
    assert "PowerShell" in stderr.getvalue()


@pytest.mark.parametrize(("elevated", "warned"), [(True, True), (False, False), (OSError("no token"), False)])
def test_windows_login_warns_when_run_as_administrator(
    monkeypatch: pytest.MonkeyPatch,
    elevated: bool | OSError,
    warned: bool,
) -> None:
    # An elevated window may belong to another Windows account, whose profile
    # and DPAPI key the client's server never sees.
    def is_elevated() -> bool:
        if isinstance(elevated, OSError):
            raise elevated
        return elevated

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(private_fs, "is_elevated", is_elevated)
    stderr = io.StringIO()

    _warn_if_elevated(stderr)

    assert ("администратора" in stderr.getvalue()) is warned


# --- console encoding


@pytest.mark.parametrize("name", ["stdout", "stderr"])
def test_windows_output_streams_are_switched_to_utf8(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, name, io.TextIOWrapper(raw, encoding="cp1252"))

    kwork_mcp.use_utf8_streams()
    stream = getattr(sys, name)
    stream.write("сервер запущен ✓")
    stream.flush()

    assert raw.getvalue().decode("utf-8") == "сервер запущен ✓"


def test_unusual_output_streams_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    kwork_mcp.use_utf8_streams()

    assert sys.stdout is stream
    assert sys.stderr is stream


def test_windows_cli_entry_point_switches_to_utf8_before_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))

    bootstrap_main(["--help"])
    sys.stdout.flush()

    assert "login" in raw.getvalue().decode("utf-8")


# --- the real Win32 calls


def _grant(path: Path, grant: str) -> None:
    subprocess.run(["icacls", str(path), "/grant", grant], check=True, capture_output=True)


def _validation_diagnostic(caught: pytest.ExceptionInfo[GatewayError]) -> str | None:
    return caught.value.diagnostic


@windows_only
def test_windows_state_directory_gets_an_owner_only_protected_dacl(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    # Everyone may write in the parent and would inherit into a plain mkdir.
    _grant(shared, "*S-1-1-0:(OI)(CI)F")

    state = ensure_secure_directory(shared / "nested" / "state")

    assert state.is_dir()
    assert private_fs.directory_problem(state) is None
    assert private_fs.directory_problem(shared / "nested") is None


@windows_only
def test_windows_rejects_a_state_directory_that_others_can_open(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _grant(state, "*S-1-1-0:(OI)(CI)F")

    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(state)

    assert _validation_diagnostic(caught) == "state_directory_acl_not_private"


@windows_only
def test_windows_rejects_a_junction_as_the_state_directory(tmp_path: Path) -> None:
    target = ensure_secure_directory(tmp_path / "target")
    junction = tmp_path / "junction"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], check=True, capture_output=True)

    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(junction)

    assert _validation_diagnostic(caught) == "state_directory_final_symlink"


@windows_only
def test_windows_rejects_ambiguous_state_paths(tmp_path: Path) -> None:
    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(tmp_path / "NUL")

    assert _validation_diagnostic(caught) == "state_directory_path_invalid"


def _save(store: SecureTokenStore, scope: str, record: TokenRecord) -> None:
    lock = store.acquire_lock(scope)
    try:
        store.save_locked(scope, record)
    finally:
        store.release_lock(lock)


def _load(store: SecureTokenStore, scope: str) -> TokenRecord | None:
    lock = store.acquire_lock(scope)
    try:
        return store.load_locked(scope)
    finally:
        store.release_lock(lock)


@windows_only
def test_windows_token_file_is_encrypted_and_bound_to_its_account(tmp_path: Path) -> None:
    store = SecureTokenStore(tmp_path / "state")
    record = TokenRecord.create(user_id=42, username="found-user", token="stored-token-sentinel")
    _save(store, "account-42", record)
    stored = tmp_path / "state" / "tokens" / "account-42.json"

    assert b"stored-token-sentinel" not in stored.read_bytes()
    assert _load(store, "account-42") == record

    (tmp_path / "state" / "tokens" / "account-77.json").write_bytes(stored.read_bytes())
    with pytest.raises(GatewayError) as caught:
        _load(store, "account-77")
    # A record DPAPI cannot open here asks for a new login, not a config fix.
    assert caught.value.code is ErrorCode.AUTH_EXPIRED
    assert caught.value.diagnostic == "token_sealed_for_another_user"


@windows_only
def test_windows_rejects_a_token_file_that_others_can_read(tmp_path: Path) -> None:
    store = SecureTokenStore(tmp_path / "state")
    _save(store, "account-42", TokenRecord.create(user_id=42, username="found-user", token="stored-token-sentinel"))
    _grant(tmp_path / "state" / "tokens" / "account-42.json", "*S-1-5-32-545:(R)")

    with pytest.raises(GatewayError) as caught:
        _load(store, "account-42")

    assert _validation_diagnostic(caught) == "private_file_acl_not_private"


@windows_only
def test_windows_rejects_a_token_lock_that_others_can_open(tmp_path: Path) -> None:
    store = SecureTokenStore(tmp_path / "state")
    store.release_lock(store.acquire_lock("account-42"))
    _grant(tmp_path / "state" / "tokens" / "account-42.lock", "*S-1-5-32-545:(R)")

    with pytest.raises(GatewayError) as caught:
        store.acquire_lock("account-42")

    assert _validation_diagnostic(caught) == "token_lock_acl_not_private"


@windows_only
def test_windows_rejects_a_ledger_that_others_can_open(config_factory: Callable[..., KworkConfig]) -> None:
    config = config_factory()
    store = CoordinationStore(config)
    _grant(store.path, "*S-1-5-32-545:(R)")

    with pytest.raises(GatewayError) as caught:
        CoordinationStore(config)

    assert _validation_diagnostic(caught) == "coordination_db_permissions"


@windows_only
@pytest.mark.asyncio
async def test_windows_rejects_a_writer_lock_that_others_can_open(
    config_factory: Callable[..., KworkConfig],
) -> None:
    store = CoordinationStore(config_factory())
    async with store.writer_guard("account-42"):
        pass
    (lock,) = store.path.parent.glob("writer-*.lock")
    _grant(lock, "*S-1-5-32-545:(R)")

    with pytest.raises(GatewayError) as caught:
        async with store.writer_guard("account-42"):
            pass

    assert _validation_diagnostic(caught) == "writer_lock_acl_not_private"


@windows_only
def test_windows_replace_waits_out_a_brief_sharing_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "new"
    target = tmp_path / "current"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    real_replace = private_fs.windows.replace
    attempts = 0

    def busy_twice(src: str, dst: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            # ERROR_SHARING_VIOLATION, as when an antivirus holds the file.
            raise OSError(None, "busy", None, 32)
        real_replace(src, dst)

    monkeypatch.setattr(private_fs.windows, "replace", busy_twice)

    private_fs.replace(str(source), target)

    assert attempts == 3
    assert target.read_bytes() == b"new"


@windows_only
def test_windows_rejects_a_state_directory_that_passes_access_on(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    # Inherit-only: Users get nothing on the directory, but read on every new file.
    _grant(state, "*S-1-5-32-545:(OI)(CI)(IO)(R)")

    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(state)

    assert _validation_diagnostic(caught) == "state_directory_acl_not_private"


@pytest.fixture
def outside_profile(tmp_path: Path) -> Iterator[Path]:
    """A fresh directory name at the drive root, outside the user's profile."""

    path = Path(tmp_path.anchor) / f"kwork-mcp-test-{uuid.uuid4().hex}"
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@windows_only
def test_windows_accepts_a_state_directory_it_creates_outside_the_profile(outside_profile: Path) -> None:
    state = ensure_secure_directory(outside_profile / "state")

    assert private_fs.directory_problem(state) is None


@windows_only
def test_windows_rejects_a_state_directory_under_a_folder_others_may_rename(outside_profile: Path) -> None:
    outside_profile.mkdir()
    _grant(outside_profile, "*S-1-5-11:(OI)(CI)M")

    with pytest.raises(GatewayError) as caught:
        ensure_secure_directory(outside_profile / "state")

    assert _validation_diagnostic(caught) == "state_directory_untrusted_writable_ancestor"
