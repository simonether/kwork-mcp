from __future__ import annotations

import importlib.metadata
import inspect
from typing import Any
from unittest.mock import AsyncMock

import pytest
from kwork import Kwork
from kwork.web_client import KworkWebClient
from pydantic import ValidationError

from kwork_mcp.contracts import (
    EXPECTED_CONTRACT_FINGERPRINT,
    ROUTE_PARAMS,
    UPSTREAM_VERSION,
    WORKER_ORDER_STATUSES,
    enforce_route_params,
    signature_fingerprint,
    verify_upstream_contract,
)
from kwork_mcp.errors import ContractDriftError, classify_upstream_error
from kwork_mcp.models import ErrorCode
from kwork_mcp.security import sanitize_external
from kwork_mcp.upstream import SecureKworkWebClient


def test_real_distribution_and_semantic_signatures_are_pinned() -> None:
    assert importlib.metadata.version("kwork") == "0.2.0" == UPSTREAM_VERSION
    status = verify_upstream_contract()
    assert status.verified is True
    assert status.signature_fingerprint == signature_fingerprint()
    assert len(status.signature_fingerprint) == 64
    assert (
        status.signature_fingerprint
        == EXPECTED_CONTRACT_FINGERPRINT
        == "4b69481dd47f1fdfb333afa161e1caf7e6d74e25d3bfbb079df5559702e3cac5"
    )
    assert tuple(inspect.signature(Kwork.get_worker_orders).parameters) == ("self",)
    assert tuple(inspect.signature(Kwork.web_login).parameters) == (
        "self",
        "url_to_redirect",
        "user_agent",
    )
    assert tuple(inspect.signature(Kwork.inbox_edit).parameters) == (
        "self",
        "use_token",
        "body",
        "params",
    )
    assert "comment" not in ROUTE_PARAMS["send_order_for_approval"]
    assert ROUTE_PARAMS["user_kworks"] == {
        "user_id",
        "page",
        "category_id",
        "status_id",
    }
    assert WORKER_ORDER_STATUSES == {
        1: "in_work",
        2: "arbitration",
        3: "cancelled",
        4: "on_review",
        5: "completed",
        6: "payment_required",
    }
    assert "page" not in inspect.signature(Kwork.get_worker_orders).parameters
    assert inspect.signature(SecureKworkWebClient.request) == inspect.signature(KworkWebClient.request)
    assert inspect.signature(SecureKworkWebClient._raise_on_web_error) == inspect.signature(
        KworkWebClient._raise_on_web_error
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "endpoint", "params"),
    [
        ("projects", "projects", {"categories": "all", "page": 3}),
        ("worker_orders", "workerOrders", {"filter": "all", "page": 2}),
        (
            "send_order_for_approval",
            "sendOrderForApproval",
            {"orderId": 81, "metrics[]": [1], "stageIds[]": [2], "filesIds[]": [3]},
        ),
        ("offers", "offers", {"page": 4}),
        ("offer", "offer", {"id": 17}),
        ("delete_offer", "deleteOffer", {"id": 17}),
        ("dialogs", "dialogs", {"page": 1}),
        ("inboxes", "inboxes", {"username": "fixture", "page": 2}),
        (
            "user_kworks",
            "userKworks",
            {"user_id": 42, "status_id": 3, "page": 2},
        ),
    ],
)
async def test_real_generic_wrappers_emit_exact_outbound_map(
    method_name: str,
    endpoint: str,
    params: dict[str, Any],
) -> None:
    client = Kwork("", "")
    request = AsyncMock(return_value={"success": True, "response": []})
    client.request = request
    try:
        result = await getattr(client, method_name)(use_token=True, **params)
    finally:
        await client.close()
    assert result["success"] is True
    request.assert_awaited_once_with("post", endpoint, use_token=True, **params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "args", "malformed"),
    [
        ("get_me", (), {"success": True}),
        ("get_user", (42,), {"success": True}),
        ("get_connects", (), {"success": True}),
        ("get_categories", (), {"success": True}),
        (
            "get_me",
            (),
            {
                "success": True,
                "response": {"id": "not-an-integer", "username": []},
            },
        ),
    ],
)
async def test_real_typed_helpers_classify_malformed_success_as_contract_drift(
    method_name: str,
    args: tuple[Any, ...],
    malformed: dict[str, Any],
) -> None:
    client = Kwork("", "")
    client.request = AsyncMock(return_value=malformed)
    try:
        with pytest.raises((KeyError, ValidationError)) as caught:
            await getattr(client, method_name)(*args)
    finally:
        await client.close()
    classified = classify_upstream_error(caught.value)
    assert classified.code is ErrorCode.CONTRACT_DRIFT
    assert classified.safe_to_retry is False


def test_route_allowlist_fails_loud_on_drift() -> None:
    enforce_route_params("worker_orders", {"filter": "all", "page": 1})
    with pytest.raises(ContractDriftError) as unsupported:
        enforce_route_params("send_order_for_approval", {"orderId": 1, "comment": "no"})
    assert unsupported.value.diagnostic == "unsupported_params:send_order_for_approval:comment"
    with pytest.raises(ContractDriftError) as unknown:
        enforce_route_params("future_endpoint", {})
    assert unknown.value.diagnostic == "unknown_route:future_endpoint"


def test_sanitized_fixture_preserves_unknown_fields_and_redacts_secrets() -> None:
    fixture = {
        "success": True,
        "response": [
            {
                "id": 101,
                "title": "Нужен исполнитель",
                "future_field": {"nested": [1, "kept"]},
                "access_token": "secret",
            }
        ],
        "proxy_url": "socks5://user:password@proxy.invalid",
    }
    sanitized = sanitize_external(fixture)
    assert sanitized == {
        "success": True,
        "response": [
            {
                "id": 101,
                "title": "Нужен исполнитель",
                "future_field": {"nested": [1, "kept"]},
                "access_token": "<redacted>",
            }
        ],
        "proxy_url": "<redacted>",
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-kwork.ru/login",
        "https://kwork.ru.evil.example/login",
        "http://kwork.ru/login",
        "https://user:password@kwork.ru/login",
        "https://kwork.ru:444/login",
        "https://kwork.ru/\nheader",
    ],
)
def test_secure_web_client_rejects_untrusted_login_urls(url: str) -> None:
    with pytest.raises(ContractDriftError):
        SecureKworkWebClient._validate_kwork_url(url)


@pytest.mark.parametrize(
    "redirect",
    [
        "https://evil.example/",
        "//evil.example/",
        r"/safe\..\evil",
        "relative/path",
        "/safe\nheader",
    ],
)
def test_secure_web_client_rejects_unsafe_relative_redirects(redirect: str) -> None:
    with pytest.raises(ValueError):
        SecureKworkWebClient._validate_relative_redirect(redirect)


def test_secure_web_client_accepts_only_expected_kwork_origins() -> None:
    SecureKworkWebClient._validate_kwork_url("https://kwork.ru/login")
    SecureKworkWebClient._validate_kwork_url("https://api.kwork.ru/path")
    SecureKworkWebClient._validate_relative_redirect("/exchange")
