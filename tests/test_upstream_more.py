from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from kwork.exceptions import KworkHTTPException

from kwork_mcp.errors import ContractDriftError
from kwork_mcp.upstream import (
    GatewayKworkClient,
    SecureKworkWebClient,
    get_client_token,
    set_client_token,
)


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        status: int,
        location: str | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.headers = {} if location is None else {"Location": location}
        self.read_calls = 0
        self.release_calls = 0

    async def read(self) -> bytes:
        self.read_calls += 1
        return b"body"

    def release(self) -> None:
        self.release_calls += 1


class ResponseContext:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, *_args: Any) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> ResponseContext:
        self.calls.append({"url": url, **kwargs})
        return ResponseContext(self.responses.pop(0))


@pytest.mark.asyncio
async def test_safe_get_follows_only_validated_redirects_and_reads_bodies() -> None:
    client = GatewayKworkClient("", "")
    first = FakeResponse(
        url="https://kwork.ru/auth",
        status=302,
        location="/exchange",
    )
    second = FakeResponse(url="https://kwork.ru/exchange", status=200)
    fake_session = FakeSession([first, second])
    client._session = fake_session  # type: ignore[assignment]
    try:
        final_url, status = await client.web._safe_get(
            "https://kwork.ru/auth",
            headers={"User-Agent": "fixture"},
            allow_redirects=True,
            max_redirects=2,
            request_timeout=aiohttp.ClientTimeout(total=1),
        )
    finally:
        client._session = None
    assert (final_url, status) == ("https://kwork.ru/exchange", 200)
    assert first.release_calls == second.release_calls == 1
    assert [call["allow_redirects"] for call in fake_session.calls] == [False, False]


@pytest.mark.asyncio
async def test_safe_get_can_disable_redirects_and_rejects_redirect_limit() -> None:
    client = GatewayKworkClient("", "")
    response = FakeResponse(
        url="https://kwork.ru/auth",
        status=302,
        location="/next",
    )
    client._session = FakeSession([response])  # type: ignore[assignment]
    try:
        assert await client.web._safe_get(
            "https://kwork.ru/auth",
            headers=None,
            allow_redirects=False,
            max_redirects=0,
            request_timeout=None,
        ) == ("https://kwork.ru/auth", 302)
    finally:
        client._session = None

    client = GatewayKworkClient("", "")
    client._session = FakeSession(  # type: ignore[assignment]
        [
            FakeResponse(
                url="https://kwork.ru/auth",
                status=302,
                location="/next",
            )
        ]
    )
    try:
        with pytest.raises(ContractDriftError) as caught:
            await client.web._safe_get(
                "https://kwork.ru/auth",
                headers=None,
                allow_redirects=True,
                max_redirects=0,
                request_timeout=None,
            )
        assert caught.value.diagnostic == "web_login_redirect_limit"
    finally:
        client._session = None


@pytest.mark.asyncio
async def test_safe_get_rejects_off_origin_response_and_redirect() -> None:
    client = GatewayKworkClient("", "")
    client._session = FakeSession(  # type: ignore[assignment]
        [FakeResponse(url="https://evil.example/", status=200)]
    )
    try:
        with pytest.raises(ContractDriftError) as response_error:
            await client.web._safe_get(
                "https://kwork.ru/auth",
                headers=None,
                allow_redirects=True,
                max_redirects=1,
                request_timeout=None,
            )
        assert response_error.value.diagnostic == "web_login_url_untrusted_host"
    finally:
        client._session = None

    client = GatewayKworkClient("", "")
    client._session = FakeSession(  # type: ignore[assignment]
        [
            FakeResponse(
                url="https://kwork.ru/auth",
                status=302,
                location="https://evil.example/",
            )
        ]
    )
    try:
        with pytest.raises(ContractDriftError) as redirect_error:
            await client.web._safe_get(
                "https://kwork.ru/auth",
                headers=None,
                allow_redirects=True,
                max_redirects=1,
                request_timeout=None,
            )
        assert redirect_error.value.diagnostic == "web_login_url_untrusted_host"
    finally:
        client._session = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "diagnostic"),
    [
        ({}, "getWebAuthToken_missing_response"),
        ({"response": []}, "getWebAuthToken_missing_response"),
        ({"response": {}}, "getWebAuthToken_missing_url"),
        ({"response": {"url": ""}}, "getWebAuthToken_missing_url"),
    ],
)
async def test_web_login_requires_exact_token_response_shape(
    response: dict[str, Any],
    diagnostic: str,
) -> None:
    client = GatewayKworkClient("", "")
    client.request = AsyncMock(return_value=response)  # type: ignore[method-assign]
    try:
        with pytest.raises(ContractDriftError) as caught:
            await client.web.login_via_mobile_web_auth_token()
        assert caught.value.diagnostic == diagnostic
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_web_login_normalizes_timeout_and_preserves_only_typed_fields() -> None:
    client = GatewayKworkClient("", "")
    client.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "response": {
                "url": "https://kwork.ru/auth",
                "token": 42,
                "expires_at": "never",
                "url_to_redirect": 42,
            }
        }
    )
    web = client.web
    web._safe_get = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            ("https://kwork.ru/auth-complete", 302),
            ("https://kwork.ru/exchange", 200),
        ]
    )
    try:
        result = await web.login_via_mobile_web_auth_token(
            url_to_redirect="/exchange",
            user_agent="fixture-agent",
            timeout=3.0,
        )
        assert result.token is None
        assert result.expires_at is None
        assert result.url_to_redirect is None
        assert result.final_url == "https://kwork.ru/exchange"
        assert result.status == 200
        assert web._safe_get.await_count == 2
        first_call = web._safe_get.await_args_list[0]
        assert first_call.kwargs["headers"] == {"User-Agent": "fixture-agent"}
        assert first_call.kwargs["request_timeout"].total == 3.0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_web_login_without_target_skips_second_get_and_keeps_typed_fields() -> None:
    client = GatewayKworkClient("", "")
    client.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "response": {
                "url": "https://kwork.ru/auth",
                "token": "web-token",
                "expires_at": 123,
                "url_to_redirect": "/",
            }
        }
    )
    web = client.web
    web._safe_get = AsyncMock(  # type: ignore[method-assign]
        return_value=("https://kwork.ru/complete", 200)
    )
    try:
        result = await web.login_via_mobile_web_auth_token(
            url_to_redirect=None,
            allow_redirects=False,
        )
        assert result.token == "web-token"
        assert result.expires_at == 123
        assert result.url_to_redirect == "/"
        web._safe_get.assert_awaited_once()
    finally:
        await client.close()


def test_gateway_web_property_replaces_plain_web_client_once() -> None:
    client = GatewayKworkClient("", "")
    first = client.web
    second = client.web
    assert isinstance(first, SecureKworkWebClient)
    assert second is first


@pytest.mark.asyncio
async def test_json_handler_accepts_success_and_rejects_http_failure() -> None:
    client = GatewayKworkClient("", "")
    response = SimpleNamespace(status=200)
    client._read_response_body = AsyncMock(  # type: ignore[method-assign]
        return_value=('{"success":true}', {"success": True, "response": {"id": 1}})
    )
    try:
        assert await client._handle_json_payload(  # type: ignore[arg-type]
            response,
            "project",
            method="post",
            request_params=None,
            request_body=None,
        ) == {"success": True, "response": {"id": 1}}

        response.status = 503
        client._read_response_body = AsyncMock(  # type: ignore[method-assign]
            return_value=("temporarily unavailable", {"success": False})
        )
        with pytest.raises(KworkHTTPException) as caught:
            await client._handle_json_payload(  # type: ignore[arg-type]
                response,
                "project",
                method="get",
                request_params={"cookie": "secret"},
                request_body={"proxy": "secret"},
            )
        assert caught.value.status == 503
        assert caught.value.request_params == {"cookie": "<redacted>"}
        assert caught.value.request_body == {"proxy": "<redacted>"}
    finally:
        await client.close()


def test_token_accessors_and_none_redaction() -> None:
    client = GatewayKworkClient("", "")
    assert get_client_token(client) is None
    set_client_token(client, "fixture")
    assert get_client_token(client) == "fixture"
    assert GatewayKworkClient._redacted_params(None) is None
