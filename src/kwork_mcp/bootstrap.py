"""Interactive, out-of-band bootstrap for the account-bound token store."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import shlex
import sys
import warnings
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self, TextIO, cast

from kwork.schema.actor import Actor
from pydantic import SecretStr, ValidationError, model_validator

from kwork_mcp.config import (
    AccountSelectionError,
    KworkConfig,
    bound_account_ids,
    contains_unsafe_text_codepoint,
    select_bound_account,
)
from kwork_mcp.coordination import CoordinationStore, StoredWrite
from kwork_mcp.errors import GatewayError, classify_upstream_error
from kwork_mcp.models import ErrorCode, WriteState
from kwork_mcp.security import (
    SecureTokenStore,
    TokenRecord,
    cancellation_safe_fd_guard,
    configure_logging,
    read_private_secret_file,
    register_redaction_secrets,
)
from kwork_mcp.session import ClientFactory
from kwork_mcp.upstream import make_client, set_client_token
from kwork_mcp.version import __version__

_HELP = """\
kwork-mcp — MCP-сервер для Kwork

Без аргументов kwork-mcp запускает MCP-сервер (stdio): так его запускает
MCP-клиент. Команды для терминала:

  kwork-mcp login            войти в Kwork и привязать аккаунт (один раз)
  kwork-mcp pending-writes   отправки с неизвестным исходом
  kwork-mcp resolve-write <write_id> succeeded|absent
                             вручную зафиксировать исход такой отправки
  kwork-mcp --version        версия

login спрашивает логин, пароль, последние 4 цифры телефона и прокси скрытым
вводом в настоящем терминале; через аргументы и .env они не принимаются. Если
KWORK_EXPECTED_USER_ID не задан, login покажет найденный аккаунт и попросит
подтвердить привязку; если задан, аккаунт обязан с ним совпасть. В конце login
печатает команды, которые подключают сервер к Claude Code и Codex.

Если вход выполнен для одного аккаунта, сервер и команды выше работают с ним
сами. Если аккаунтов несколько, укажите нужный в KWORK_EXPECTED_USER_ID.

Для kwork.com задайте KWORK_SITE=com. Аккаунт и токен у kwork.ru и kwork.com
общие, поэтому повторный вход при смене сайта не нужен.

Если существует legacy ~/.kwork_token с owner=current user и mode 0600, login
предложит проверить и импортировать его. Сервер legacy-файл никогда не
импортирует.

Отправка с неизвестным исходом (submission_unknown) блокирует новые отправки
аккаунта, пока её не сверит reconcile_write. Если сверка не приходит к выводу,
проверьте операцию на сайте Kwork и зафиксируйте исход командой resolve-write.

kwork-mcp-bootstrap — прежнее имя этих команд, оно продолжает работать.
"""

_RESOLUTION_STATES = {
    "succeeded": WriteState.RECONCILED_SUCCEEDED,
    "absent": WriteState.RECONCILED_ABSENT,
}
_CONFIRMATIONS = {"да", "д", "yes", "y"}

_BOOTSTRAP_CLOSE_TIMEOUT_SECONDS = 5.0
_BOOTSTRAP_CLOSE_CANCEL_GRACE_SECONDS = 0.1


class _BootstrapEnvironment(KworkConfig):
    """Non-secret bootstrap settings, read before the account is known.

    The steady-state rule "no credentials requires KWORK_EXPECTED_USER_ID" does
    not apply here: bootstrap prompts for credentials afterwards and can
    discover the account ID itself.
    """

    @model_validator(mode="after")
    def validate_auth_and_legacy_options(self) -> Self:
        self._validate_common_options()
        return self


def _base_bootstrap_config() -> KworkConfig:
    """Read only non-secret environment settings and override inherited auth."""

    return _BootstrapEnvironment(
        login="",
        password=SecretStr(""),
        phone_last=None,
        token=SecretStr(""),
        proxy_url=None,
        enable_writes=False,
        token_file=None,
    )


def _auth_config(
    base: KworkConfig,
    *,
    login: str = "",
    password: str = "",
    phone_last: str = "",
    proxy_url: str = "",
    token: str = "",
    expected_user_id: int | None = None,
) -> KworkConfig:
    """Construct and revalidate a fresh settings object with prompted secrets."""

    values = base.model_dump()
    if expected_user_id is not None:
        values["expected_user_id"] = expected_user_id
    values.update(
        {
            "login": login,
            "password": SecretStr(password),
            "phone_last": SecretStr(phone_last) if phone_last else None,
            "token": SecretStr(token),
            "proxy_url": SecretStr(proxy_url) if proxy_url else None,
            "enable_writes": False,
            "token_file": None,
        }
    )
    return KworkConfig(**values)


def _require_safe_identity(actor: Actor) -> tuple[int, str]:
    if actor.id is None or actor.id <= 0 or not actor.username or contains_unsafe_text_codepoint(actor.username):
        raise GatewayError(
            ErrorCode.CONTRACT_DRIFT,
            diagnostic="bootstrap_actor_missing_safe_identity",
        )
    return actor.id, actor.username


def _validate_actor(config: KworkConfig, actor: Actor) -> None:
    user_id, username = _require_safe_identity(actor)
    if user_id != config.expected_user_id:
        raise GatewayError(
            ErrorCode.ACCOUNT_MISMATCH,
            diagnostic="bootstrap_expected_user_id_mismatch",
        )
    if config.expected_username and username.casefold() != config.expected_username.casefold():
        raise GatewayError(
            ErrorCode.ACCOUNT_MISMATCH,
            diagnostic="bootstrap_expected_username_mismatch",
        )


async def _coordinated_auth_call[ResultT](
    coordinator: CoordinationStore,
    *,
    scope: str,
    route: str,
    operation: Callable[[], Awaitable[ResultT]],
) -> ResultT:
    await coordinator.acquire(scope, route)
    try:
        result = await operation()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = classify_upstream_error(exc)
        if error.retryable:
            await coordinator.record_failure(
                scope,
                route,
                retry_after_seconds=error.retry_after_seconds,
            )
        else:
            await coordinator.record_success(scope, route)
        raise error from exc
    await coordinator.record_success(scope, route)
    return result


@dataclass(frozen=True, slots=True)
class _BootstrapCloseOutcome:
    caller_cancelled: bool = False
    child_cancelled: bool = False
    timed_out: bool = False

    @property
    def cancelled(self) -> bool:
        return self.caller_cancelled or self.child_cancelled or self.timed_out


def _consume_close_task(task: asyncio.Task[Any]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


async def _close_bootstrap_client(
    client: Any,
    *,
    timeout_seconds: float | None = None,
    cancel_grace_seconds: float | None = None,
) -> _BootstrapCloseOutcome:
    """Close fully while distinguishing caller and terminal child cancellation."""

    timeout = _BOOTSTRAP_CLOSE_TIMEOUT_SECONDS if timeout_seconds is None else max(timeout_seconds, 0.0)
    cancel_grace = (
        _BOOTSTRAP_CLOSE_CANCEL_GRACE_SECONDS if cancel_grace_seconds is None else max(cancel_grace_seconds, 0.0)
    )
    close_task = asyncio.create_task(client.close())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    owner_task = asyncio.current_task()
    initial_cancelling = owner_task.cancelling() if owner_task is not None else 0
    caller_cancelled = False
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                await asyncio.shield(close_task)
            return _BootstrapCloseOutcome(caller_cancelled=caller_cancelled)
        except TimeoutError:
            break
        except asyncio.CancelledError:
            if owner_task is not None and owner_task.cancelling() > initial_cancelling:
                caller_cancelled = True
            if close_task.done() and close_task.cancelled():
                return _BootstrapCloseOutcome(
                    caller_cancelled=caller_cancelled,
                    child_cancelled=True,
                )
            caller_cancelled = True
        except Exception:
            return _BootstrapCloseOutcome(caller_cancelled=caller_cancelled)

    close_task.cancel()
    drain_deadline = loop.time() + cancel_grace
    while not close_task.done():
        remaining = drain_deadline - loop.time()
        if remaining <= 0:
            break
        try:
            async with asyncio.timeout(remaining):
                await asyncio.shield(close_task)
            break
        except TimeoutError:
            break
        except asyncio.CancelledError:
            if owner_task is not None and owner_task.cancelling() > initial_cancelling:
                caller_cancelled = True
            if close_task.done() and close_task.cancelled():
                break
        except Exception:
            break
    if not close_task.done():
        close_task.add_done_callback(_consume_close_task)
    return _BootstrapCloseOutcome(
        caller_cancelled=caller_cancelled,
        child_cancelled=close_task.cancelled(),
        timed_out=True,
    )


async def discover_account(
    config: KworkConfig,
    coordinator: CoordinationStore,
    *,
    client_factory: ClientFactory = make_client,
) -> tuple[Actor, str]:
    """Sign in once without a binding and return the account and its token.

    Nothing is stored: the caller confirms the account with the operator and
    then binds it through ``bootstrap_account`` using the returned token.
    """

    scope = config.bootstrap_scope
    client: Any = client_factory(config)
    try:
        if config.token_value:
            token = config.token_value
            set_client_token(client, token)
        else:
            token = await _coordinated_auth_call(
                coordinator,
                scope=scope,
                route="signIn",
                operation=client.get_token,
            )
            if not token:
                raise GatewayError(ErrorCode.CONTRACT_DRIFT, diagnostic="bootstrap_empty_login_token")
            register_redaction_secrets((token,))
        actor = await _coordinated_auth_call(
            coordinator,
            scope=scope,
            route="actor",
            operation=client.get_me,
        )
        _require_safe_identity(actor)
        return actor, token
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise classify_upstream_error(exc) from exc
    finally:
        close_outcome = await _close_bootstrap_client(client)
        if close_outcome.caller_cancelled:
            raise asyncio.CancelledError


async def bootstrap_account(
    config: KworkConfig,
    coordinator: CoordinationStore,
    *,
    token_store: SecureTokenStore | None = None,
    client_factory: ClientFactory = make_client,
) -> Actor:
    """Freshly authenticate and atomically replace one verified account token."""

    if config.expected_user_id is None or not config.persist_token:
        raise GatewayError(
            ErrorCode.ACCOUNT_BINDING_REQUIRED,
            diagnostic="bootstrap_requires_persisted_expected_user_id",
        )
    has_token = bool(config.token_value)
    has_login = config.fresh_credentials_available
    if has_token == has_login:
        raise GatewayError(
            ErrorCode.AUTH_REQUIRED,
            diagnostic="bootstrap_requires_exactly_one_credential_source",
        )

    scope = f"account-{config.expected_user_id}"
    store = token_store or SecureTokenStore(
        config.state_dir,
        lock_timeout=config.auth_lock_timeout,
    )
    committed = False
    post_commit_cleanup_error: GatewayError | None = None
    try:
        async with (
            coordinator.writer_guard(scope),
            cancellation_safe_fd_guard(
                lambda: store.acquire_lock(scope),
                store.release_lock,
            ),
        ):
            client: Any | None = None
            primary_error: BaseException | None = None
            try:
                client = client_factory(config)
                if has_token:
                    token = config.token_value
                    set_client_token(client, token)
                else:
                    token = await _coordinated_auth_call(
                        coordinator,
                        scope=scope,
                        route="signIn",
                        operation=client.get_token,
                    )
                    if not token:
                        raise GatewayError(
                            ErrorCode.CONTRACT_DRIFT,
                            diagnostic="bootstrap_empty_login_token",
                        )
                    register_redaction_secrets((token,))
                actor = await _coordinated_auth_call(
                    coordinator,
                    scope=scope,
                    route="actor",
                    operation=client.get_me,
                )
                _validate_actor(config, actor)
                store.save_locked(
                    scope,
                    TokenRecord.create(
                        user_id=cast(int, actor.id),
                        username=cast(str, actor.username),
                        token=token,
                        proxy_url=config.proxy_value,
                    ),
                )
                committed = True
            except asyncio.CancelledError as exc:
                primary_error = exc
                raise
            except Exception as exc:
                primary_error = classify_upstream_error(exc)
                raise primary_error from exc
            finally:
                if client is not None:
                    close_outcome = await _close_bootstrap_client(client)
                    if close_outcome.cancelled:
                        if committed:
                            raise GatewayError(
                                ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
                                reconciliation_required=True,
                                diagnostic=(
                                    "bootstrap_close_timeout_after_store_commit"
                                    if close_outcome.timed_out
                                    else "bootstrap_cancelled_after_store_commit"
                                ),
                            )
                        if (
                            isinstance(primary_error, GatewayError)
                            and primary_error.code is ErrorCode.CREDENTIAL_UPDATE_UNKNOWN
                        ):
                            raise primary_error
                        if close_outcome.caller_cancelled:
                            raise asyncio.CancelledError
    except asyncio.CancelledError as exc:
        if committed:
            raise GatewayError(
                ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
                reconciliation_required=True,
                diagnostic="bootstrap_cancelled_during_lock_release_after_store_commit",
            ) from exc
        raise
    except GatewayError:
        raise
    except Exception:
        if not committed:
            raise
        post_commit_cleanup_error = GatewayError(
            ErrorCode.CREDENTIAL_UPDATE_UNKNOWN,
            reconciliation_required=True,
            diagnostic="bootstrap_lock_release_failed_after_store_commit",
        )
    if post_commit_cleanup_error is not None:
        raise post_commit_cleanup_error
    return actor


def _hidden_prompt(
    prompt: str,
    *,
    stream: TextIO,
    getpass_fn: Callable[..., str],
) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass_fn(prompt, stream=stream).strip()


def _visible_prompt(
    prompt: str,
    *,
    stdin: TextIO,
    stderr: TextIO,
) -> str:
    """Read a non-secret answer from the exact TTY streams already verified."""

    stderr.write(prompt)
    stderr.flush()
    value = stdin.readline()
    if value == "":
        raise EOFError
    return value.strip()


_PROMPTED_FIELD_LABELS = {
    "proxy_url": "Proxy URL",
    "phone_last": "последние 4 цифры телефона",
    "login": "Kwork login",
    "password": "Kwork password",
    "token": "legacy token",
}


def _field_label(field: str) -> str:
    return _PROMPTED_FIELD_LABELS.get(field, f"KWORK_{field.upper()}")


def _terminal_safe(text: str) -> str:
    """Escape controls and bidi overrides; the result stays valid JSON."""

    return "".join(
        f"\\u{ord(char):04x}" if contains_unsafe_text_codepoint(char) and char != "\n" else char for char in text
    )


def _write_admin_account(config: KworkConfig) -> int:
    if config.expected_user_id is not None:
        return config.expected_user_id
    return select_bound_account(config.state_dir)


def _write_summary(record: StoredWrite) -> dict[str, Any]:
    payload = json.loads(record.payload_json)
    request = payload.get("request") if isinstance(payload, dict) else None
    return {
        "write_id": record.write_id,
        "action": record.action.value,
        "state": record.state.value,
        "prepared_at": datetime.fromtimestamp(record.prepared_at, tz=UTC).isoformat(),
        "updated_at": datetime.fromtimestamp(record.updated_at, tz=UTC).isoformat(),
        "site": record.prepared_site,
        "request": request,
    }


async def _run_write_admin(
    argv: Sequence[str],
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Operator commands for writes that read-back cannot settle."""

    command, *rest = argv
    try:
        config = _base_bootstrap_config()
        account_id = _write_admin_account(config)
        scope = f"account-{account_id}"
        ledger = config.state_dir / "coordination.sqlite3"
        if not ledger.is_file():
            # Opening would create an empty ledger and report nothing pending.
            stderr.write(
                f"Ledger не найден: {_terminal_safe(str(ledger))}. Запустите команду с тем же "
                "KWORK_STATE_DIR и KWORK_* настройками, что и MCP server.\n"
            )
            return 1
        coordinator = CoordinationStore(config)
        if command == "pending-writes":
            if rest:
                stderr.write("Ошибка: pending-writes не принимает аргументы.\n")
                return 2
            records = await coordinator.list_unresolved_writes(scope)
            listing = {
                "schema_version": "1.0",
                "account_id": account_id,
                "writes": [_write_summary(record) for record in records],
            }
            stdout.write(_terminal_safe(json.dumps(listing, ensure_ascii=False, sort_keys=True)) + "\n")
            return 0

        if len(rest) != 2 or rest[1] not in _RESOLUTION_STATES:
            stderr.write("Использование: kwork-mcp resolve-write <write_id> succeeded|absent\n")
            return 2
        write_id, outcome = rest
        if not (stdin.isatty() and stderr.isatty()):
            stderr.write("Ошибка: resolve-write требует настоящий интерактивный TTY.\n")
            return 2
        record = await coordinator.get_write(write_id, scope=scope)
        if record is None:
            raise GatewayError(ErrorCode.NOT_FOUND, diagnostic="write_id_not_found")
        if record.state is not WriteState.SUBMISSION_UNKNOWN:
            stderr.write(f"Запись {write_id} уже в состоянии {record.state.value}; разрешать нечего.\n")
            return 1
        stderr.write(_terminal_safe(json.dumps(_write_summary(record), ensure_ascii=False, indent=2)) + "\n")
        stderr.write(
            f"Убедитесь на kwork.{_terminal_safe(record.prepared_site)}, что операция "
            + ("выполнена" if outcome == "succeeded" else "НЕ выполнена")
            + ". Ошибочное решение может привести к дублю или потере записи.\n"
        )
        answer = _visible_prompt(
            f"Зафиксировать {write_id} как {outcome}? Введите «да»: ",
            stdin=stdin,
            stderr=stderr,
        )
        if answer.casefold() not in _CONFIRMATIONS:
            stderr.write("Отменено; запись не изменена.\n")
            return 1
        resolved = await coordinator.operator_resolve_write(
            write_id=write_id,
            scope=scope,
            state=_RESOLUTION_STATES[outcome],
        )
        stdout.write(
            json.dumps(
                {"write_id": resolved.write_id, "state": resolved.state.value},
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
        return 0
    except (EOFError, KeyboardInterrupt):
        stderr.write("Отменено; запись не изменена.\n")
        return 130
    except AccountSelectionError as exc:
        stderr.write(f"Не выполнено: {exc}.\n")
        return 2
    except ValidationError:
        stderr.write("Ошибка конфигурации: проверьте KWORK_EXPECTED_USER_ID и KWORK_STATE_DIR.\n")
        return 2
    except GatewayError as error:
        stderr.write(f"Не выполнено: {error.code.value}: {error.safe_message}\n")
        return 1


_SITE_URL = "https://simonether.github.io/kwork-mcp/#start"


def _connection_instructions(actor: Actor, config: KworkConfig, *, legacy_file_retained: bool) -> str:
    """What to run next, with every setting the server needs to find this account."""

    try:
        several_accounts = len(bound_account_ids(config.state_dir)) > 1
    except AccountSelectionError:
        # The token is already stored; naming the account is always correct.
        several_accounts = True
    environment: list[str] = []
    if several_accounts:
        environment.append(f"KWORK_EXPECTED_USER_ID={actor.id}")
    if config.site != "ru":
        environment.append(f"KWORK_SITE={config.site}")
    # A client may start the server without XDG_STATE_HOME, so anything but
    # the plain default location is passed explicitly.
    if config.state_dir != Path.home() / ".local" / "state" / "kwork-mcp":
        environment.append(f"KWORK_STATE_DIR={config.state_dir}")
    name = "kwork" if config.site == "ru" else f"kwork-{config.site}"
    claude = ["claude", "mcp", "add", name, "--scope", "user"]
    codex = ["codex", "mcp", "add", name]
    for item in environment:
        claude += ["-e", item]
        codex += ["--env", item]
    server = ["--", "uvx", f"kwork-mcp@{__version__}"]
    lines = [
        f"Аккаунт {_terminal_safe(str(actor.username))} (user_id {actor.id}) подключён.",
        "",
        "Добавьте kwork-mcp в агента. Claude Code:",
        "  " + _terminal_safe(shlex.join(claude + server)),
        "Codex:",
        "  " + _terminal_safe(shlex.join(codex + server)),
        f"Claude Desktop, Cursor и другие клиенты: {_SITE_URL}",
    ]
    if legacy_file_retained:
        lines += ["", "Файл ~/.kwork_token не удалён; если он больше не нужен, удалите его сами."]
    return "\n".join(lines) + "\n"


async def run_bootstrap_cli(
    argv: Sequence[str],
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    getpass_fn: Callable[..., str] = getpass.getpass,
    client_factory: ClientFactory = make_client,
    home_dir: Path | None = None,
) -> int:
    """Run the human CLI; prompts go to stderr, results and next steps to stdout."""

    args = list(argv)
    if args in (["--help"], ["-h"]):
        stdout.write(_HELP)
        return 0
    if args == ["--version"]:
        stdout.write(f"{__version__}\n")
        return 0
    if args and args[0] in {"pending-writes", "resolve-write"}:
        return await _run_write_admin(args, stdin=stdin, stdout=stdout, stderr=stderr)
    if args == ["login"]:
        args = []
    if args:
        # Never echo argv: it may hold a credential pasted by mistake.
        stderr.write(
            "Ошибка: неизвестная команда. Доступны login, pending-writes и resolve-write; "
            "логин и пароль через аргументы не принимаются. Справка: kwork-mcp --help.\n"
        )
        return 2
    try:
        interactive = stdin.isatty() and stderr.isatty()
    except Exception:
        with contextlib.suppress(Exception):
            stderr.write("Ошибка: не удалось проверить, что login запущен в интерактивном терминале (TTY).\n")
        return 2
    if not interactive:
        stderr.write("Ошибка: login запускается только в интерактивном терминале (TTY).\n")
        return 2

    try:
        base = _base_bootstrap_config()
        coordinator = CoordinationStore(base)
        store = SecureTokenStore(
            base.state_dir,
            lock_timeout=base.auth_lock_timeout,
        )

        legacy_path = (home_dir or Path.home()) / ".kwork_token"
        legacy_exists = False
        try:
            legacy_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise GatewayError(
                ErrorCode.VALIDATION,
                diagnostic=f"legacy_token_probe:{type(exc).__name__}",
            ) from exc
        else:
            legacy_exists = True

        used_legacy = False
        if legacy_exists:
            choice = _visible_prompt(
                "Проверить и импортировать защищённый legacy ~/.kwork_token? [y/N]: ",
                stdin=stdin,
                stderr=stderr,
            )
            used_legacy = choice.casefold() in {"y", "yes", "д", "да"}

        if used_legacy:
            legacy_token = read_private_secret_file(legacy_path)
            if legacy_token is None:  # pragma: no cover - raced disappearance
                raise GatewayError(
                    ErrorCode.AUTH_REQUIRED,
                    diagnostic="legacy_token_disappeared",
                )
            proxy_url = _hidden_prompt(
                "Proxy URL (необязательно): ",
                stream=stderr,
                getpass_fn=getpass_fn,
            )
            config = _auth_config(
                base,
                token=legacy_token,
                proxy_url=proxy_url,
            )
        else:
            login = _hidden_prompt(
                "Kwork login: ",
                stream=stderr,
                getpass_fn=getpass_fn,
            )
            password = _hidden_prompt(
                "Kwork password: ",
                stream=stderr,
                getpass_fn=getpass_fn,
            )
            phone_last = _hidden_prompt(
                "Последние 4 цифры телефона (необязательно): ",
                stream=stderr,
                getpass_fn=getpass_fn,
            )
            proxy_url = _hidden_prompt(
                "Proxy URL (необязательно): ",
                stream=stderr,
                getpass_fn=getpass_fn,
            )
            config = _auth_config(
                base,
                login=login,
                password=password,
                phone_last=phone_last,
                proxy_url=proxy_url,
            )

        configure_logging(config)
        if config.expected_user_id is None:
            discovered, token = await discover_account(
                config,
                coordinator,
                client_factory=client_factory,
            )
            answer = _visible_prompt(
                f"Найден аккаунт Kwork: {discovered.username} (user_id={discovered.id}). Привязать его? [y/N]: ",
                stdin=stdin,
                stderr=stderr,
            )
            if answer.casefold() not in _CONFIRMATIONS:
                stderr.write("Вход отменён: сохранённый токен не изменён.\n")
                return 1
            config = _auth_config(
                base,
                token=token,
                proxy_url=config.proxy_value or "",
                expected_user_id=discovered.id,
            )
        actor = await bootstrap_account(
            config,
            coordinator,
            token_store=store,
            client_factory=client_factory,
        )
        stdout.write(_connection_instructions(actor, config, legacy_file_retained=used_legacy))
        return 0
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        stderr.write("Вход отменён: сохранённый токен не изменён.\n")
        return 130
    except ValidationError as exc:
        invalid = sorted({_field_label(str(error["loc"][0])) for error in exc.errors() if error.get("loc")})
        if invalid:
            stderr.write("Ошибка в настройках входа: некорректное значение — " + ", ".join(invalid) + ".\n")
        else:
            stderr.write(
                "Ошибка в настройках входа. Проверьте KWORK_EXPECTED_USER_ID, KWORK_STATE_DIR и KWORK_PERSIST_TOKEN.\n"
            )
        return 2
    except GatewayError as error:
        stderr.write(f"Вход не выполнен: {error.code.value}: {error.safe_message}\n")
        return 1
    except Exception:
        stderr.write("Вход не выполнен из-за внутренней ошибки.\n")
        return 1


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    code = asyncio.run(
        run_bootstrap_cli(
            args,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
    )
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
