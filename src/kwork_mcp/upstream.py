"""Pinned upstream client adapter with lossless API-error capture."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import ClientResponse
from kwork import Kwork
from kwork.exceptions import KworkHTTPException
from kwork.web_client import KworkWebClient, WebLoginResult

from kwork_mcp.config import KworkConfig
from kwork_mcp.errors import ContractDriftError

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_WEB_REDIRECT_LIMIT = 10


class SecureKworkWebClient(KworkWebClient):
    """Kwork web client that never follows a token-bearing URL off Kwork."""

    @staticmethod
    def _raise_on_web_error(resp: dict[str, Any], *, where: str) -> None:
        """Preserve structured web errors for the gateway taxonomy."""

        payload = resp.get("json")
        if isinstance(payload, dict) and payload.get("success") is False:
            raw_status = resp.get("status")
            status = raw_status if isinstance(raw_status, int) and not isinstance(raw_status, bool) else None
            raise KworkHTTPException(
                "Kwork web API rejected request",
                status=status,
                endpoint=where,
                response_json=payload,
            )

    @staticmethod
    def _validate_kwork_url(value: str) -> None:
        if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ContractDriftError("web_login_url_control_character")
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme.casefold() != "https":
            raise ContractDriftError("web_login_url_requires_https")
        if hostname != "kwork.ru" and not hostname.endswith(".kwork.ru"):
            raise ContractDriftError("web_login_url_untrusted_host")
        if parsed.username is not None or parsed.password is not None:
            raise ContractDriftError("web_login_url_contains_userinfo")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ContractDriftError("web_login_url_invalid_port") from exc
        if port not in {None, 443}:
            raise ContractDriftError("web_login_url_nonstandard_port")

    @staticmethod
    def _validate_relative_redirect(value: str | None) -> None:
        if value is None:
            return
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("url_to_redirect contains control characters")
        parsed = urlsplit(value)
        if (
            parsed.scheme
            or parsed.netloc
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
            or "\\" in value
        ):
            raise ValueError("url_to_redirect must be an absolute-path reference")

    @staticmethod
    def _origin(value: str) -> tuple[str, str, int]:
        parsed = urlsplit(value)
        return (
            parsed.scheme.casefold(),
            (parsed.hostname or "").casefold().rstrip("."),
            parsed.port or 443,
        )

    async def _safe_get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        allow_redirects: bool,
        max_redirects: int,
        request_timeout: aiohttp.ClientTimeout | None,
    ) -> tuple[str, int]:
        current = url
        for redirect_count in range(max_redirects + 1):
            self._validate_kwork_url(current)
            async with self._api.session.get(
                current,
                headers=headers,
                allow_redirects=False,
                timeout=request_timeout,
            ) as response:
                response_url = str(response.url)
                status = response.status
                location = response.headers.get("Location")
                response.release()
            self._validate_kwork_url(response_url)
            if not allow_redirects or status not in _REDIRECT_STATUSES or not location:
                return response_url, status
            if redirect_count >= max_redirects:
                raise ContractDriftError("web_login_redirect_limit")
            current = urljoin(response_url, location)
        raise ContractDriftError("web_login_redirect_limit")  # pragma: no cover

    async def request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        data: Any = None,
        json_data: Any | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
        timeout: aiohttp.ClientTimeout | float | None = None,  # noqa: ASYNC109 -- upstream contract
    ) -> dict[str, Any]:
        """Perform a web request without allowing credentials to cross an origin.

        aiohttp's automatic redirect handling can resend POST bodies and CSRF
        headers on 307/308. All redirects are therefore inspected before another
        request is made, and redirects after a mutating request are rejected
        because the first request may already have taken effect.
        """

        if path_or_url.startswith(("http://", "https://")):
            url = path_or_url
        else:
            url = urljoin(self._base_url, path_or_url.lstrip("/"))
        self._validate_kwork_url(url)
        initial_origin = self._origin(url)
        method_upper = method.upper()

        request_headers = dict(headers or {})
        if "X-Requested-With" in request_headers:
            self._maybe_add_csrf_headers(url, request_headers)
        effective_timeout = self._api._normalize_timeout(timeout) if timeout is not None else None

        current_url = url
        current_params = params
        for redirect_count in range(_WEB_REDIRECT_LIMIT + 1):
            request_kwargs: dict[str, Any] = {
                "method": method_upper,
                "url": current_url,
                "params": current_params,
                "data": data,
                "json": json_data,
                "headers": request_headers or None,
                "allow_redirects": False,
            }
            if json_data is not None:
                request_kwargs.pop("data", None)
            if effective_timeout is not None:
                request_kwargs["timeout"] = effective_timeout

            async with self._api.session.request(**request_kwargs) as response:
                response_url = str(response.url)
                status = response.status
                response_headers = {key: value for key, value in response.headers.items()}
                location = response.headers.get("Location")
                should_follow = (
                    allow_redirects and status in _REDIRECT_STATUSES and isinstance(location, str) and bool(location)
                )
                if should_follow:
                    response.release()
                    text = ""
                else:
                    text = await response.text(errors="replace")
            self._validate_kwork_url(response_url)

            if should_follow:
                target_url = urljoin(response_url, location)
                self._validate_kwork_url(target_url)
                if self._origin(target_url) != initial_origin:
                    raise ContractDriftError("web_request_cross_origin_redirect")
                if method_upper not in {"GET", "HEAD"}:
                    raise ContractDriftError("web_write_redirect_rejected")
                if redirect_count >= _WEB_REDIRECT_LIMIT:
                    raise ContractDriftError("web_request_redirect_limit")
                current_url = target_url
                current_params = None
                continue

            content_type = response_headers.get("Content-Type", "").partition(";")[0].strip().casefold()
            parsed_json: Any | None = None
            if content_type == "application/json" or content_type.endswith("+json"):
                try:
                    parsed_json = json.loads(text)
                except json.JSONDecodeError:
                    parsed_json = None
            return {
                "status": status,
                "url": response_url,
                "headers": response_headers,
                "text": text,
                "json": parsed_json,
            }
        raise ContractDriftError("web_request_redirect_limit")  # pragma: no cover

    async def login_via_mobile_web_auth_token(
        self,
        *,
        url_to_redirect: str | None = "/",
        user_agent: str | None = None,
        allow_redirects: bool = True,
        max_redirects: int = 10,
        timeout: aiohttp.ClientTimeout | float | None = None,  # noqa: ASYNC109 -- upstream contract
    ) -> WebLoginResult:
        self._validate_relative_redirect(url_to_redirect)
        token_response = await self._api.request(
            "post",
            "getWebAuthToken",
            use_token=True,
            retry=False,
            url_to_redirect=url_to_redirect,
        )
        payload_raw = token_response.get("response")
        if not isinstance(payload_raw, dict):
            raise ContractDriftError("getWebAuthToken_missing_response")
        login_url = payload_raw.get("url")
        if not isinstance(login_url, str) or not login_url:
            raise ContractDriftError("getWebAuthToken_missing_url")
        self._validate_kwork_url(login_url)

        headers = {"User-Agent": user_agent} if user_agent else None
        effective_timeout = self._api._normalize_timeout(timeout) if timeout is not None else None
        final_url, status = await self._safe_get(
            login_url,
            headers=headers,
            allow_redirects=allow_redirects,
            max_redirects=max_redirects,
            request_timeout=effective_timeout,
        )
        if url_to_redirect:
            target_url = urljoin(self._base_url, url_to_redirect.lstrip("/"))
            final_url, status = await self._safe_get(
                target_url,
                headers=headers,
                allow_redirects=allow_redirects,
                max_redirects=max_redirects,
                request_timeout=effective_timeout,
            )
        token = payload_raw.get("token")
        expires_at = payload_raw.get("expires_at")
        returned_redirect = payload_raw.get("url_to_redirect")
        return WebLoginResult(
            token=token if isinstance(token, str) else None,
            expires_at=expires_at if isinstance(expires_at, int) else None,
            login_url=login_url,
            url_to_redirect=(returned_redirect if isinstance(returned_redirect, str) else None),
            final_url=final_url,
            status=status,
        )


class GatewayKworkClient(Kwork):
    """Keep ``success:false`` payloads that upstream 0.2.0 otherwise discards."""

    @property
    def web(self) -> SecureKworkWebClient:
        if not isinstance(self._web_client, SecureKworkWebClient):
            self._web_client = SecureKworkWebClient(self)
        return self._web_client

    async def _handle_json_payload(
        self,
        resp: ClientResponse,
        endpoint: str,
        *,
        method: str,
        request_params: dict[str, Any] | None,
        request_body: Any | None,
    ) -> dict[str, Any]:
        body_text, data = await self._read_response_body(resp)
        safe_params = self._redacted_params(request_params)
        safe_body = self._redacted_body(request_body)
        if resp.status < 200 or resp.status >= 300:
            raise KworkHTTPException(
                f"HTTP {resp.status} for {method.upper()} /{endpoint}",
                status=resp.status,
                method=method.upper(),
                endpoint=endpoint,
                response_text=self._truncate(body_text),
                response_json=data,
                request_params=safe_params,
                request_body=safe_body,
            )
        if data is None:
            raise KworkHTTPException(
                f"Non-JSON response from /{endpoint}",
                status=resp.status,
                method=method.upper(),
                endpoint=endpoint,
                response_text=self._truncate(body_text),
                request_params=safe_params,
                request_body=safe_body,
            )
        if data.get("success") is not True:
            raise KworkHTTPException(
                f"Kwork API rejected /{endpoint}",
                status=resp.status,
                method=method.upper(),
                endpoint=endpoint,
                response_text=self._truncate(body_text),
                response_json=data,
                request_params=safe_params,
                request_body=safe_body,
            )
        return data

    @staticmethod
    def _redacted_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
        if params is None:
            return None
        return {
            key: (
                "<redacted>"
                if any(
                    part in key.casefold()
                    for part in (
                        "token",
                        "password",
                        "login",
                        "authorization",
                        "cookie",
                        "proxy",
                    )
                )
                else value
            )
            for key, value in params.items()
        }

    @staticmethod
    def _redacted_body(body: Any) -> dict[str, Any] | None:
        if not isinstance(body, dict):
            return None
        return {
            key: (
                "<redacted>"
                if any(
                    part in key.casefold()
                    for part in (
                        "token",
                        "password",
                        "login",
                        "authorization",
                        "cookie",
                        "proxy",
                    )
                )
                else value
            )
            for key, value in body.items()
        }


def make_client(config: KworkConfig) -> GatewayKworkClient:
    return GatewayKworkClient(
        login=config.login,
        password=config.password_value,
        proxy=config.proxy_value,
        phone_last=config.phone_last_value,
        timeout=config.timeout,
        retry_max_attempts=1,
        retry_backoff_base=config.retry_backoff_base,
        retry_backoff_max=config.retry_backoff_max,
        retry_jitter=0.1,
        relogin_on_auth_error=False,
    )


def set_client_token(client: Kwork, token: str) -> None:
    # kwork 0.2.0 has no public token setter. Runtime contract tests pin this field.
    client._token = token


def get_client_token(client: Kwork) -> str | None:
    return client._token
