"""Authenticated, account-bound Kwork session with coordinated read retries."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar, cast

from kwork import Kwork
from kwork.schema.actor import Actor
from loguru import logger
from pydantic import SecretStr

from kwork_mcp.config import (
    KworkConfig,
    contains_unsafe_text_codepoint,
    proxy_redaction_secrets,
)
from kwork_mcp.coordination import CoordinationStore
from kwork_mcp.errors import GatewayError, classify_upstream_error, is_auth_error
from kwork_mcp.models import ErrorCode
from kwork_mcp.security import (
    SecureTokenStore,
    TokenRecord,
    cancellation_safe_fd_guard,
    register_redaction_secrets,
)
from kwork_mcp.upstream import get_client_token, make_client, set_client_token

ReturnT = TypeVar("ReturnT")
ClientFactory = Callable[[KworkConfig], Kwork]
ClientCall = Callable[[Kwork], Awaitable[ReturnT]]
BeforeRemoteAttempt = Callable[[], Awaitable[None]]

_CANONICAL_ROUTES = {
    "account": "actor",
    "auth-identity": "actor",
    "write-identity": "actor",
    "auth-login": "signIn",
    "connects": "projects",
    "user-by-username": "userByUsername",
    "user-search": "userSearch",
    "exchange-info": "exchangeInfo",
    "worker-orders": "workerOrders",
    "order-details": "getOrderDetails",
    "dialog-messages": "inboxes",
    "kworks": "kworksStatusList",
    "user-kworks": "userKworks",
    "kwork-details": "getKworkDetailsExtra",
    "favorite-categories": "favoriteCategories",
    "web-login": "getWebAuthToken",
    "write-delete-offer": "deleteOffer",
    "write-send-message": "inboxCreate",
    "write-edit-message": "inboxEdit",
    "write-delete-message": "inboxDelete",
    "write-mark-dialog-read": "inboxRead",
    "write-order-approval": "sendOrderForApproval",
    "write-start-kwork": "startKwork",
    "write-pause-kwork": "pauseKwork",
    "write-offer-page": "web:exchange:new-offer",
    "write-offer-faq": "web:quick-faq-init",
    "write-offer-draft": "web:offer-draft",
    "write-offer-template-check": "web:offer-template-check",
    "write-offer-final": "web:offer-create",
}


def _canonical_route(route: str) -> str:
    return _CANONICAL_ROUTES.get(route, route)


class KworkSessionManager:
    """Own a lazily authenticated client and verify its immutable account ID."""

    def __init__(
        self,
        config: KworkConfig,
        coordinator: CoordinationStore,
        *,
        client_factory: ClientFactory = make_client,
        token_store: SecureTokenStore | None = None,
    ) -> None:
        self.config = config
        self.coordinator = coordinator
        self._client_factory = client_factory
        self._token_store = token_store or SecureTokenStore(
            config.state_dir,
            lock_timeout=config.auth_lock_timeout,
        )
        self._client: Kwork | None = None
        self._actor: Actor | None = None
        self._session_account_id: int | None = None
        self._web_logged_in = False
        self._auth_lock = asyncio.Lock()
        self._client_use_lock = asyncio.Lock()
        self._exclusive_owner: asyncio.Task[object] | None = None
        self._explicit_token_rejected = False
        self._stored_token_rejected = False
        self._rejected_token_hashes: set[str] = set()
        self._runtime_redaction_secrets: set[str] = set(config.redaction_secrets)
        self._active_proxy_url: str | None = None

    @asynccontextmanager
    async def _client_guard(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is not None and self._exclusive_owner is task:
            yield
            return
        async with self._client_use_lock:
            yield

    @asynccontextmanager
    async def exclusive_client(self) -> AsyncIterator[None]:
        """Prevent relogin/close while a multi-step remote write is running."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("exclusive_client requires an asyncio task")
        if self._exclusive_owner is task:
            yield
            return
        async with self._client_use_lock:
            self._exclusive_owner = task
            try:
                yield
            finally:
                self._exclusive_owner = None

    @property
    def actor(self) -> Actor | None:
        return self._actor

    @property
    def redaction_secrets(self) -> tuple[str, ...]:
        return tuple(self._runtime_redaction_secrets)

    @property
    def scope(self) -> str:
        if self._actor is not None and self._actor.id is not None:
            return f"account-{self._actor.id}"
        return self.config.bootstrap_scope

    def _register_runtime_credentials(
        self,
        *,
        token: str | None = None,
        proxy_url: str | None = None,
    ) -> None:
        values = [token or "", proxy_url or ""]
        if proxy_url:
            values.extend(proxy_redaction_secrets(proxy_url))
        secrets = tuple(value for value in values if value)
        self._runtime_redaction_secrets.update(secrets)
        register_redaction_secrets(secrets)

    async def ensure_client(self) -> Kwork:
        if self._client is not None:
            return self._client
        async with self._auth_lock:
            if self._client is None:
                await self._authenticate_locked(force_fresh=False)
            return cast(Kwork, self._client)

    async def _close_client(self, client: Kwork | None) -> None:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    def _validate_actor(self, actor: Actor) -> None:
        if actor.id is None or actor.id <= 0 or not actor.username or contains_unsafe_text_codepoint(actor.username):
            raise GatewayError(
                ErrorCode.CONTRACT_DRIFT,
                diagnostic="actor_missing_stable_identity",
            )
        if self._session_account_id is not None and actor.id != self._session_account_id:
            raise GatewayError(
                ErrorCode.ACCOUNT_MISMATCH,
                diagnostic="session_account_changed",
            )
        expected_id = self.config.expected_user_id
        expected_username = self.config.expected_username
        if expected_id is not None and actor.id != expected_id:
            raise GatewayError(
                ErrorCode.ACCOUNT_MISMATCH,
                diagnostic=f"expected_id={expected_id};actual_id={actor.id}",
            )
        if expected_username and actor.username.casefold() != expected_username.lstrip("@").casefold():
            raise GatewayError(
                ErrorCode.ACCOUNT_MISMATCH,
                diagnostic="expected_username_mismatch",
            )
        self._session_account_id = actor.id

    async def _validate_token_client(self, client: Kwork) -> Actor:
        route = _canonical_route("auth-identity")
        await self.coordinator.acquire(self.config.bootstrap_scope, route)
        try:
            actor = await client.get_me()
        except Exception as exc:
            error = classify_upstream_error(exc)
            if error.retryable:
                await self.coordinator.record_failure(
                    self.config.bootstrap_scope,
                    route,
                    retry_after_seconds=error.retry_after_seconds,
                )
            else:
                await self.coordinator.record_success(self.config.bootstrap_scope, route)
            raise error from exc
        await self.coordinator.record_success(self.config.bootstrap_scope, route)
        self._validate_actor(actor)
        return actor

    async def _try_token(
        self,
        token: str,
        *,
        source: str,
        expected_record: TokenRecord | None = None,
    ) -> tuple[Kwork, Actor] | None:
        self._register_runtime_credentials(
            token=token,
            proxy_url=(expected_record.proxy_url if expected_record is not None else self.config.proxy_value),
        )
        client_config = self.config
        if expected_record is not None and expected_record.proxy_url != self.config.proxy_value:
            values = self.config.model_dump()
            values["proxy_url"] = (
                SecretStr(expected_record.proxy_url) if expected_record.proxy_url is not None else None
            )
            client_config = KworkConfig(**values)
        client: Kwork | None = None
        try:
            client = self._client_factory(client_config)
            set_client_token(client, token)
            actor = await self._validate_token_client(client)
            actor_username = actor.username
            if not actor_username:
                raise GatewayError(
                    ErrorCode.CONTRACT_DRIFT,
                    diagnostic="actor_missing_username_after_validation",
                )
            if expected_record is not None and (expected_record.user_id != actor.id):
                raise GatewayError(
                    ErrorCode.ACCOUNT_MISMATCH,
                    diagnostic="stored_token_identity_changed",
                )
            self._rejected_token_hashes.clear()
            self._stored_token_rejected = False
            logger.info("Authenticated Kwork session source={} account_id={}", source, actor.id)
            return client, actor
        except asyncio.CancelledError:
            await self._close_client(client)
            raise
        except GatewayError as error:
            await self._close_client(client)
            if error.code in {ErrorCode.AUTH_EXPIRED, ErrorCode.AUTH_REQUIRED}:
                self._remember_rejected_token(token)
                logger.warning("Rejected invalid Kwork token source={}", source)
                return None
            if error.code is ErrorCode.ACCOUNT_MISMATCH:
                self._remember_rejected_token(token)
            raise
        except Exception as exc:
            await self._close_client(client)
            raise classify_upstream_error(exc) from exc

    async def _fresh_login(self) -> tuple[Kwork, Actor]:
        if not self.config.login or not self.config.password_value:
            raise GatewayError(ErrorCode.AUTH_REQUIRED, diagnostic="fresh_credentials_unavailable")
        client = self._client_factory(self.config)
        route = _canonical_route("auth-login")
        await self.coordinator.acquire(self.config.bootstrap_scope, route)
        login_request_completed = False
        try:
            token = await client.get_token()
            login_request_completed = True
            if not token:
                raise GatewayError(ErrorCode.CONTRACT_DRIFT, diagnostic="empty_login_token")
            self._register_runtime_credentials(
                token=token,
                proxy_url=self.config.proxy_value,
            )
            await self.coordinator.record_success(
                self.config.bootstrap_scope,
                route,
            )
            actor = await self._validate_token_client(client)
            self._rejected_token_hashes.clear()
            self._stored_token_rejected = False
            return client, actor
        except asyncio.CancelledError:
            await self._close_client(client)
            raise
        except Exception as exc:
            await self._close_client(client)
            error = classify_upstream_error(exc)
            if isinstance(exc, GatewayError):
                error = exc
            if not login_request_completed:
                if error.retryable:
                    await self.coordinator.record_failure(
                        self.config.bootstrap_scope,
                        route,
                        retry_after_seconds=error.retry_after_seconds,
                    )
                else:
                    await self.coordinator.record_success(
                        self.config.bootstrap_scope,
                        route,
                    )
            else:
                await self.coordinator.record_success(
                    self.config.bootstrap_scope,
                    route,
                )
            raise error from exc

    @staticmethod
    def _token_digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _remember_rejected_token(self, token: str | None) -> None:
        if token:
            self._rejected_token_hashes.add(self._token_digest(token))

    def _token_was_rejected(self, token: str) -> bool:
        return self._token_digest(token) in self._rejected_token_hashes

    async def _activate_candidate_locked(
        self,
        client: Kwork,
        actor: Actor,
        *,
        scope: str,
        persist: bool,
        proxy_url: str | None,
    ) -> None:
        try:
            if persist:
                token = get_client_token(client)
                if not token or actor.id is None or not actor.username:
                    raise GatewayError(
                        ErrorCode.CONTRACT_DRIFT,
                        diagnostic="authenticated_client_missing_token",
                    )
                self._token_store.save_locked(
                    scope,
                    TokenRecord.create(
                        user_id=actor.id,
                        username=actor.username,
                        token=token,
                        proxy_url=proxy_url,
                    ),
                )
        except BaseException:
            await self._close_client(client)
            self._client = None
            self._actor = None
            self._active_proxy_url = None
            raise
        self._client, self._actor = client, actor
        self._active_proxy_url = proxy_url
        self._rejected_token_hashes.clear()
        self._stored_token_rejected = False

    async def _authenticate_locked(
        self,
        *,
        force_fresh: bool,
        rejected_token: str | None = None,
    ) -> None:
        scope = self.config.bootstrap_scope
        can_use_store = self.config.persist_token and self.config.token_cache_is_bound
        rejected_any = force_fresh or self._explicit_token_rejected or self._stored_token_rejected
        async with cancellation_safe_fd_guard(
            lambda: self._token_store.acquire_lock(scope),
            self._token_store.release_lock,
        ):
            if force_fresh:
                if self.config.token_value:
                    self._explicit_token_rejected = True
                self._remember_rejected_token(rejected_token)
                if can_use_store:
                    peer_record = self._token_store.load_locked(scope)
                    if peer_record is not None:
                        if self._token_was_rejected(peer_record.token):
                            self._stored_token_rejected = True
                            rejected_any = True
                        else:
                            try:
                                candidate = await self._try_token(
                                    peer_record.token,
                                    source="peer_refreshed_store",
                                    expected_record=peer_record,
                                )
                            except GatewayError as error:
                                if error.code is ErrorCode.ACCOUNT_MISMATCH:
                                    self._stored_token_rejected = True
                                raise
                            if candidate is not None:
                                client, actor = candidate
                                await self._activate_candidate_locked(
                                    client,
                                    actor,
                                    scope=scope,
                                    persist=peer_record.username != actor.username,
                                    proxy_url=peer_record.proxy_url,
                                )
                                return
                            self._stored_token_rejected = True
                            rejected_any = True
            else:
                explicit = self.config.token_value
                if explicit and not self._explicit_token_rejected:
                    try:
                        candidate = await self._try_token(explicit, source="explicit")
                    except GatewayError as error:
                        if error.code is ErrorCode.ACCOUNT_MISMATCH:
                            self._explicit_token_rejected = True
                        raise
                    if candidate is not None:
                        client, actor = candidate
                        await self._activate_candidate_locked(
                            client,
                            actor,
                            scope=scope,
                            persist=can_use_store,
                            proxy_url=self.config.proxy_value,
                        )
                        return
                    self._explicit_token_rejected = True
                    rejected_any = True

                if can_use_store:
                    record = self._token_store.load_locked(scope)
                    if record is not None and self._token_was_rejected(record.token):
                        self._stored_token_rejected = True
                        rejected_any = True
                        record = None
                    if record is not None:
                        try:
                            candidate = await self._try_token(
                                record.token,
                                source="account_store",
                                expected_record=record,
                            )
                        except GatewayError as error:
                            if error.code is ErrorCode.ACCOUNT_MISMATCH:
                                self._stored_token_rejected = True
                            raise
                        if candidate is not None:
                            client, actor = candidate
                            await self._activate_candidate_locked(
                                client,
                                actor,
                                scope=scope,
                                persist=record.username != actor.username,
                                proxy_url=record.proxy_url,
                            )
                            return
                        self._stored_token_rejected = True
                        rejected_any = True

            if not self.config.fresh_credentials_available:
                error_code = ErrorCode.AUTH_EXPIRED if rejected_any else ErrorCode.AUTH_REQUIRED
                diagnostic = (
                    "stored_token_rejected_no_credentials"
                    if error_code is ErrorCode.AUTH_EXPIRED
                    else "stored_token_missing"
                )
                raise GatewayError(error_code, diagnostic=diagnostic)
            client, actor = await self._fresh_login()
            await self._activate_candidate_locked(
                client,
                actor,
                scope=scope,
                persist=can_use_store,
                proxy_url=self.config.proxy_value,
            )
            logger.info("Authenticated Kwork session source=fresh account_id={}", actor.id)

    async def _persist_current_token_locked(self, scope: str) -> None:
        if (
            not self.config.persist_token
            or not self.config.token_cache_is_bound
            or self._client is None
            or self._actor is None
        ):
            return
        token = get_client_token(self._client)
        if not token or self._actor.id is None or not self._actor.username:
            return
        self._token_store.save_locked(
            scope,
            TokenRecord.create(
                user_id=self._actor.id,
                username=self._actor.username,
                token=token,
                proxy_url=self._active_proxy_url,
            ),
        )

    async def relogin(self, *, stale_client: Kwork | None = None) -> Kwork:
        """Discard stale token sources and use credentials exactly once.

        Concurrent reads can observe the same expired client. Only the first one
        performs a fresh login; followers reuse that replacement instead of
        invalidating it again.
        """

        async with self._client_guard(), self._auth_lock:
            if stale_client is not None and self._client is not None and self._client is not stale_client:
                return self._client
            old_client = self._client
            rejected_client = stale_client or old_client
            rejected_token = get_client_token(rejected_client) if rejected_client is not None else None
            self._remember_rejected_token(rejected_token)
            self._client = None
            self._actor = None
            self._active_proxy_url = None
            self._web_logged_in = False
            await self._close_client(old_client)
            await self._authenticate_locked(
                force_fresh=True,
                rejected_token=rejected_token,
            )
            return cast(Kwork, self._client)

    async def call_read(self, route: str, operation: ClientCall[ReturnT]) -> ReturnT:
        result, _scope = await self.call_read_scoped(route, operation)
        return result

    async def call_read_scoped(
        self,
        route: str,
        operation: ClientCall[ReturnT],
        *,
        expected_scope: str | None = None,
    ) -> tuple[ReturnT, str]:
        """Run a read and return its atomically authenticated account scope."""

        route = _canonical_route(route)
        refreshed = False
        transient_attempt = 0
        while True:
            remote_error: Exception | None = None
            async with self._client_guard():
                client = await self.ensure_client()
                scope = self.scope
                if expected_scope is not None and scope != expected_scope:
                    raise GatewayError(
                        ErrorCode.VALIDATION,
                        diagnostic="authenticated_scope_mismatch",
                    )
                await self.coordinator.acquire(scope, route)
                try:
                    result = await operation(client)
                except Exception as exc:
                    remote_error = exc
            if remote_error is not None:
                if is_auth_error(remote_error) and not refreshed:
                    refreshed = True
                    # A concrete 401 proves the half-open transport probe reached
                    # Kwork. Clear its shared circuit before refreshing auth so
                    # this logical read can retry without admitting concurrent
                    # probes from the same process.
                    await self.coordinator.record_success(scope, route)
                    await self.relogin(stale_client=client)
                    continue
                error = classify_upstream_error(remote_error)
                if error.retryable:
                    await self.coordinator.record_failure(
                        scope,
                        route,
                        retry_after_seconds=error.retry_after_seconds,
                    )
                else:
                    await self.coordinator.record_success(scope, route)
                transient_attempt += 1
                if not error.retryable or not error.safe_to_retry or transient_attempt >= self.config.read_attempts:
                    raise error from remote_error
                delay = min(
                    self.config.retry_backoff_base * (2 ** (transient_attempt - 1)),
                    self.config.retry_backoff_max,
                )
                if error.retry_after_seconds is not None:
                    delay = min(
                        max(delay, error.retry_after_seconds),
                        self.config.retry_backoff_max,
                    )
                delay_with_jitter = min(
                    delay + random.uniform(0.0, delay * 0.1),
                    self.config.retry_backoff_max,
                )
                await asyncio.sleep(delay_with_jitter)
                continue
            await self.coordinator.record_success(scope, route)
            return result, scope

    async def verify_account_identity(self) -> Actor:
        """Freshly verify the configured account binding without requiring writes."""

        if self.config.expected_user_id is None:
            raise GatewayError(
                ErrorCode.ACCOUNT_BINDING_REQUIRED,
                diagnostic="expected_user_id_missing",
            )
        actor = await self.call_read("write-identity", lambda client: client.get_me())
        self._validate_actor(actor)
        self._actor = actor
        return actor

    async def verify_write_identity(self) -> Actor:
        if not self.config.enable_writes:
            raise GatewayError(ErrorCode.WRITE_DISABLED, diagnostic="enable_writes=false")
        return await self.verify_account_identity()

    async def call_write_step(
        self,
        route: str,
        operation: ClientCall[ReturnT],
        *,
        before_remote_attempt: BeforeRemoteAttempt | None = None,
    ) -> ReturnT:
        """Run one upstream write request with no automatic retry."""

        route = _canonical_route(route)
        remote_error: Exception | None = None
        async with self._client_guard():
            client = await self.ensure_client()
            scope = self.scope
            await self.coordinator.acquire(scope, route)
            if before_remote_attempt is not None:
                await before_remote_attempt()
            try:
                result = await operation(client)
            except Exception as exc:
                remote_error = exc
        if remote_error is not None:
            error = classify_upstream_error(remote_error)
            if error.retryable:
                await self.coordinator.record_failure(
                    scope,
                    route,
                    retry_after_seconds=error.retry_after_seconds,
                )
            else:
                await self.coordinator.record_success(scope, route)
            raise error from remote_error
        await self.coordinator.record_success(scope, route)
        return result

    async def ensure_web_client(self) -> Kwork:
        async with self._client_guard():
            client = await self.ensure_client()
            if self._web_logged_in:
                return client
            async with self._auth_lock:
                current = self._client
                if current is None:  # pragma: no cover - guarded invariant
                    raise GatewayError(
                        ErrorCode.INTERNAL,
                        diagnostic="web_login_client_missing",
                    )
                client = current
                if not self._web_logged_in:
                    route = _canonical_route("web-login")
                    await self.coordinator.acquire(self.scope, route)
                    try:
                        result = await client.web_login(url_to_redirect="/exchange")
                        if result.status is None or not 200 <= result.status < 400:
                            raise GatewayError(
                                ErrorCode.AUTH_EXPIRED,
                                diagnostic=f"web_login_status={result.status}",
                            )
                    except Exception as exc:
                        error = classify_upstream_error(exc)
                        if error.retryable:
                            await self.coordinator.record_failure(
                                self.scope,
                                route,
                                retry_after_seconds=error.retry_after_seconds,
                            )
                        else:
                            await self.coordinator.record_success(self.scope, route)
                        raise error from exc
                    await self.coordinator.record_success(self.scope, route)
                    self._web_logged_in = True
            return client

    async def close(self) -> None:
        async with self._client_guard(), self._auth_lock:
            client = self._client
            self._client = None
            self._actor = None
            self._active_proxy_url = None
            self._web_logged_in = False
            await self._close_client(client)
