from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest
from kwork.exceptions import KworkException, KworkHTTPException, KworkRetryExceeded
from pydantic import ValidationError

from kwork_mcp.config import KworkConfig
from kwork_mcp.errors import (
    GatewayError,
    classify_upstream_error,
    is_auth_error,
    is_transient_error,
)
from kwork_mcp.models import (
    ErrorCode,
    KnowledgeState,
    MarkDialogReadRequest,
    ResultEnvelope,
    ResultMeta,
    WriteAction,
)
from kwork_mcp.upstream import GatewayKworkClient, make_client


@pytest.mark.parametrize(
    ("exception", "code", "retryable"),
    [
        (
            KworkHTTPException(
                "captcha",
                status=200,
                response_json={"error_code": 118},
            ),
            ErrorCode.CAPTCHA,
            False,
        ),
        (KworkHTTPException("unauthorized", status=401), ErrorCode.AUTH_EXPIRED, False),
        (KworkHTTPException("forbidden", status=403), ErrorCode.PERMISSION, False),
        (KworkHTTPException("missing", status=404), ErrorCode.NOT_FOUND, False),
        (KworkHTTPException("duplicate", status=409), ErrorCode.DUPLICATE, False),
        (KworkHTTPException("limited", status=429), ErrorCode.RATE_LIMIT, True),
        (KworkHTTPException("failed", status=503), ErrorCode.UPSTREAM_UNAVAILABLE, True),
        (KworkHTTPException("bad", status=422), ErrorCode.PERMISSION, False),
        (KworkHTTPException("odd", status=200), ErrorCode.CONTRACT_DRIFT, False),
        (TimeoutError(), ErrorCode.TIMEOUT, True),
        (aiohttp.ClientConnectionError(), ErrorCode.UPSTREAM_UNAVAILABLE, True),
        (KworkException("project closed"), ErrorCode.CLOSED_PROJECT, False),
        (KworkException("opaque"), ErrorCode.UPSTREAM_UNAVAILABLE, False),
        (KeyError("response"), ErrorCode.CONTRACT_DRIFT, False),
        (RuntimeError("internal"), ErrorCode.INTERNAL, False),
    ],
)
def test_error_taxonomy(
    exception: BaseException,
    code: ErrorCode,
    retryable: bool,
) -> None:
    error = classify_upstream_error(exception)
    assert error.code is code
    assert error.retryable is retryable
    if str(exception):
        assert str(exception) not in error.safe_message
    info = error.to_info("correlation")
    assert info.code is code
    assert info.correlation_id == "correlation"


def test_retry_exhaustion_and_error_predicates() -> None:
    exhausted = KworkRetryExceeded(
        "retry",
        attempts=3,
        last_error=TimeoutError(),
    )
    assert classify_upstream_error(exhausted).code is ErrorCode.TIMEOUT
    assert is_auth_error(GatewayError(ErrorCode.AUTH_EXPIRED))
    assert not is_auth_error(GatewayError(ErrorCode.PERMISSION))
    assert is_transient_error(TimeoutError())
    assert not is_transient_error(GatewayError(ErrorCode.DUPLICATE))


def test_result_envelope_state_invariants() -> None:
    meta = ResultMeta(observed_at="2026-07-27T00:00:00Z", correlation_id="c")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="unknown_error requires error"):
        ResultEnvelope[dict[str, Any]](
            knowledge_state=KnowledgeState.UNKNOWN_ERROR,
            summary="error",
            meta=meta,
        )
    with pytest.raises(ValidationError, match="known_data requires data"):
        ResultEnvelope[dict[str, Any]](
            knowledge_state=KnowledgeState.KNOWN_DATA,
            summary="missing",
            meta=meta,
        )
    envelope = ResultEnvelope[dict[str, Any]](
        knowledge_state=KnowledgeState.KNOWN_EMPTY,
        summary="empty",
        data=None,
        meta=meta,
    )
    assert envelope.error is None


def test_write_ids_use_strict_integer_validation_and_reject_bool() -> None:
    with pytest.raises(ValidationError):
        MarkDialogReadRequest(
            action=WriteAction.MARK_DIALOG_READ,
            user_id=True,
        )


def test_adapter_redacts_request_diagnostics() -> None:
    assert GatewayKworkClient._redacted_params({"token": "secret", "page": 1}) == {"token": "<redacted>", "page": 1}
    assert GatewayKworkClient._redacted_body({"password": "secret", "text": "ok"}) == {
        "password": "<redacted>",
        "text": "ok",
    }
    assert GatewayKworkClient._redacted_body("not-a-map") is None


@pytest.mark.asyncio
async def test_adapter_rejects_failure_and_empty_json_payloads() -> None:
    client = GatewayKworkClient("", "")
    response = type("Response", (), {"status": 200})()
    client._read_response_body = AsyncMock(  # type: ignore[method-assign]
        return_value=('{"success": false}', {"success": False, "error_code": 118})
    )
    try:
        with pytest.raises(KworkHTTPException) as rejected:
            await client._handle_json_payload(  # type: ignore[arg-type]
                response,
                "projects",
                method="post",
                request_params={"token": "secret"},
                request_body={"password": "secret"},
            )
        assert rejected.value.response_json == {"success": False, "error_code": 118}
        assert rejected.value.request_params == {"token": "<redacted>"}
        assert rejected.value.request_body == {"password": "<redacted>"}

        client._read_response_body = AsyncMock(return_value=("not-json", None))  # type: ignore[method-assign]
        with pytest.raises(KworkHTTPException, match="Non-JSON"):
            await client._handle_json_payload(  # type: ignore[arg-type]
                response,
                "projects",
                method="post",
                request_params=None,
                request_body=None,
            )
    finally:
        await client.close()


def test_make_client_disables_upstream_retries(
    config_factory: Callable[..., KworkConfig],
) -> None:
    client = make_client(config_factory(timeout=11.0))
    try:
        assert client._retry_max_attempts == 1
        assert client._timeout.total == 11.0
        assert client._relogin_on_auth_error is False
    finally:
        asyncio.run(client.close())
