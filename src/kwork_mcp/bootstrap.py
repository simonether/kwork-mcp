"""Interactive, out-of-band bootstrap for the account-bound token store."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import json
import sys
import warnings
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from kwork.schema.actor import Actor
from pydantic import SecretStr, ValidationError

from kwork_mcp.config import KworkConfig, contains_unsafe_text_codepoint
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError, classify_upstream_error
from kwork_mcp.models import ErrorCode
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
kwork-mcp-bootstrap — безопасная одноразовая авторизация Kwork

Команда не принимает credentials через argv или .env. Заранее задайте только
KWORK_EXPECTED_USER_ID и, при необходимости, KWORK_STATE_DIR. Login/password,
последние цифры телефона и proxy вводятся скрыто через настоящий TTY.

Если существует legacy ~/.kwork_token с owner=current user и mode 0600, CLI
предложит явно проверить и импортировать его. Обычный MCP server legacy-файл
никогда не импортирует.
"""

_BOOTSTRAP_CLOSE_TIMEOUT_SECONDS = 5.0
_BOOTSTRAP_CLOSE_CANCEL_GRACE_SECONDS = 0.1


def _base_bootstrap_config() -> KworkConfig:
    """Read only non-secret environment settings and override inherited auth."""

    return KworkConfig(
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
) -> KworkConfig:
    """Construct and revalidate a fresh settings object with prompted secrets."""

    values = base.model_dump()
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


def _validate_actor(config: KworkConfig, actor: Actor) -> None:
    if actor.id is None or actor.id <= 0 or not actor.username or contains_unsafe_text_codepoint(actor.username):
        raise GatewayError(
            ErrorCode.CONTRACT_DRIFT,
            diagnostic="bootstrap_actor_missing_safe_identity",
        )
    if actor.id != config.expected_user_id:
        raise GatewayError(
            ErrorCode.ACCOUNT_MISMATCH,
            diagnostic="bootstrap_expected_user_id_mismatch",
        )
    if config.expected_username and actor.username.casefold() != config.expected_username.casefold():
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
    """Run the human CLI while keeping stdout machine-safe and allowlisted."""

    if list(argv) in (["--help"], ["-h"]):
        stdout.write(_HELP)
        return 0
    if list(argv) == ["--version"]:
        stdout.write(f"{__version__}\n")
        return 0
    if argv:
        stderr.write("Ошибка: kwork-mcp-bootstrap не принимает параметры авторизации через argv.\n")
        return 2
    try:
        interactive = stdin.isatty() and stderr.isatty()
    except Exception:
        with contextlib.suppress(Exception):
            stderr.write("Ошибка: bootstrap не смог проверить интерактивный TTY.\n")
        return 2
    if not interactive:
        stderr.write("Ошибка: bootstrap требует настоящий интерактивный TTY.\n")
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
        actor = await bootstrap_account(
            config,
            coordinator,
            token_store=store,
            client_factory=client_factory,
        )
        result: dict[str, Any] = {
            "schema_version": "1.0",
            "verified": True,
            "account": {
                "user_id": actor.id,
                "username": actor.username,
            },
            "environment": {
                "KWORK_EXPECTED_USER_ID": str(actor.id),
                "KWORK_ENABLE_WRITES": "false",
                "KWORK_PERSIST_TOKEN": "true",
            },
            "credential_store": "account_scoped_token",
        }
        if used_legacy:
            result["legacy_file_retained"] = True
        stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        stderr.write("Bootstrap отменён без изменения credential store.\n")
        return 130
    except ValidationError:
        stderr.write(
            "Ошибка безопасной конфигурации bootstrap. Проверьте KWORK_EXPECTED_USER_ID, "
            "KWORK_STATE_DIR и KWORK_PERSIST_TOKEN.\n"
        )
        return 2
    except GatewayError as error:
        stderr.write(f"Bootstrap не выполнен: {error.code.value}: {error.safe_message}\n")
        return 1
    except Exception:
        stderr.write("Bootstrap не выполнен из-за внутренней ошибки.\n")
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
