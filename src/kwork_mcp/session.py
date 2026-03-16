from __future__ import annotations

import asyncio
import contextlib
import os

from kwork import Kwork
from loguru import logger

from kwork_mcp.config import KworkConfig
from kwork_mcp.rate_limiter import InProcessRateLimiter


def _set_client_token(client: Kwork, token: str) -> None:
    client._token = token


def _get_client_token(client: Kwork) -> str | None:
    return client._token


class KworkSessionManager:
    """Manages a single pykwork client with lazy auth and auto-relogin."""

    def __init__(self, config: KworkConfig) -> None:
        self._config = config
        self._client: Kwork | None = None
        self._web_logged_in = False
        self._lock = asyncio.Lock()
        self._rate_limiter = InProcessRateLimiter(
            rps=config.rps_limit,
            burst=config.burst_limit,
        )

    async def ensure_client(self) -> Kwork:
        """Return authenticated client, creating/authenticating lazily."""
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            self._client = await self._create_and_auth()
            return self._client

    async def ensure_web_client(self) -> Kwork:
        """Return client with web session established (for submit_offer)."""
        client = await self.ensure_client()
        if not self._web_logged_in:
            async with self._lock:
                if not self._web_logged_in:
                    await self._rate_limiter.wait_and_acquire()
                    await client.web_login(url_to_redirect="/exchange")
                    self._web_logged_in = True
                    logger.info("Web session established")
        return client

    async def rate_limit(self) -> None:
        await self._rate_limiter.wait_and_acquire()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._web_logged_in = False

    async def get_exchange_info(self) -> dict:
        """Fetch exchange statistics via raw HTTP (bypasses pykwork's JSON handler)."""
        from kwork.api import AUTH_HEADER

        client = await self.ensure_client()
        token = _get_client_token(client)
        resp = await client.session.post(
            "https://api.kwork.ru/exchangeInfo",
            headers={"Authorization": AUTH_HEADER},
            data={"token": token},
        )
        return await resp.json(content_type=None)

    def _make_client(self) -> Kwork:
        cfg = self._config
        return Kwork(
            login=cfg.login or "",
            password=cfg.password.get_secret_value() or "",
            proxy=cfg.proxy_url,
            phone_last=cfg.phone_last,
        )

    async def _create_and_auth(self) -> Kwork:
        cfg = self._config

        # Priority 1: explicit token from env
        if cfg.token and cfg.token.get_secret_value():
            logger.info("Using KWORK_TOKEN from environment")
            client = self._make_client()
            _set_client_token(client, cfg.token.get_secret_value())
            return client

        # Priority 2: token from file
        if cfg.token_file.exists():
            try:
                saved_token = cfg.token_file.read_text().strip()
                if saved_token:
                    logger.info("Restoring token from {}", cfg.token_file)
                    client = self._make_client()
                    _set_client_token(client, saved_token)
                    return client
            except OSError:
                logger.warning("Failed to read token file {}", cfg.token_file)

        # Priority 3: fresh login
        if not cfg.login or not cfg.password.get_secret_value():
            msg = (
                "Авторизация не настроена. Укажите KWORK_TOKEN или KWORK_LOGIN + KWORK_PASSWORD в переменных окружения."
            )
            raise RuntimeError(msg)

        logger.info("Signing in as {}", cfg.login)
        client = self._make_client()
        await self._rate_limiter.wait_and_acquire()

        token = await client.get_token()
        if token:
            self._persist_token(token)
        return client

    async def relogin(self) -> Kwork:
        """Force re-authentication (e.g. after 401)."""
        async with self._lock:
            if self._client:
                with contextlib.suppress(Exception):
                    await self._client.close()
            self._client = None
            self._web_logged_in = False
            if self._config.token_file.exists():
                with contextlib.suppress(OSError):
                    self._config.token_file.unlink()
            self._client = await self._create_and_auth()
            return self._client

    def _persist_token(self, token: str) -> None:
        try:
            target = self._config.token_file
            fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, token.encode())
            finally:
                os.close(fd)
            logger.info("Token saved to {}", target)
        except OSError as exc:
            logger.warning("Failed to persist token: {}", exc)
