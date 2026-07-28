"""Fail-loud semantic contract for the exact upstream ``kwork==0.2.0``."""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
from collections.abc import Callable
from typing import Any, Literal

from kwork import Kwork
from kwork.schema.actor import Actor
from kwork.schema.connects import Connects
from kwork.schema.dialog import DialogMessage
from kwork.schema.inbox import InboxMessage
from kwork.schema.project import WantWorker
from kwork.schema.user import User
from kwork.web_client import KworkWebClient

from kwork_mcp.errors import ContractDriftError
from kwork_mcp.models import ContractStatusData

UPSTREAM_DISTRIBUTION: Literal["kwork"] = "kwork"
UPSTREAM_VERSION: Literal["0.2.0"] = "0.2.0"
EXPECTED_CONTRACT_FINGERPRINT = "4b69481dd47f1fdfb333afa161e1caf7e6d74e25d3bfbb079df5559702e3cac5"
WORKER_ORDER_STATUSES: dict[int, str] = {
    1: "in_work",
    2: "arbitration",
    3: "cancelled",
    4: "on_review",
    5: "completed",
    6: "payment_required",
}

# Derived from the OpenAPI snapshot associated with upstream commit 723829a.
# Generic **params wrappers cannot protect callers from misspelled/unsupported fields,
# so the gateway adapter and contract tests use this allowlist.
ROUTE_PARAMS: dict[str, frozenset[str]] = {
    "projects": frozenset(
        {
            "categories",
            "price_from",
            "price_to",
            "hiring_from",
            "kworks_filter_from",
            "kworks_filter_to",
            "page",
            "query",
            "attributes",
            "offers",
        }
    ),
    "project": frozenset({"id"}),
    "user_by_username": frozenset({"username", "with_hidden"}),
    "user_search": frozenset({"query", "page"}),
    "dialogs": frozenset({"page", "excludedIds"}),
    "inboxes": frozenset({"username", "page"}),
    "inbox_read": frozenset({"user_id", "messages"}),
    "inbox_edit_query": frozenset({"id", "uploaded_files", "reply_message_id"}),
    "offers": frozenset({"page"}),
    "offer": frozenset({"id"}),
    "delete_offer": frozenset({"id"}),
    "worker_orders": frozenset({"filter", "page"}),
    "get_order_details": frozenset({"orderId"}),
    "send_order_for_approval": frozenset({"orderId", "metrics[]", "stageIds[]", "filesIds[]"}),
    "get_kwork_details_extra": frozenset({"id"}),
    "start_kwork": frozenset({"kwork_id"}),
    "pause_kwork": frozenset({"kwork_id"}),
    "favorite_categories": frozenset(),
    "notifications": frozenset(),
    "kworks_status_list": frozenset(),
    "user_kworks": frozenset({"user_id", "page", "category_id", "status_id"}),
    "exchange_info": frozenset(),
}


def enforce_route_params(route: str, params: dict[str, Any]) -> None:
    allowed = ROUTE_PARAMS.get(route)
    if allowed is None:
        raise ContractDriftError(f"unknown_route:{route}")
    unexpected = set(params) - allowed
    if unexpected:
        raise ContractDriftError(f"unsupported_params:{route}:{','.join(sorted(unexpected))}")


_EXPECTED_SIGNATURES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "__init__": (
        ("login", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("password", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("proxy", "POSITIONAL_OR_KEYWORD", "None"),
        ("phone_last", "POSITIONAL_OR_KEYWORD", "None"),
        ("api_host", "POSITIONAL_OR_KEYWORD", "'https://api.kwork.ru/{}'"),
        ("timeout", "KEYWORD_ONLY", "30.0"),
        ("retry_max_attempts", "KEYWORD_ONLY", "1"),
        ("retry_backoff_base", "KEYWORD_ONLY", "0.5"),
        ("retry_backoff_max", "KEYWORD_ONLY", "8.0"),
        ("retry_jitter", "KEYWORD_ONLY", "0.1"),
        ("retry_statuses", "KEYWORD_ONLY", "None"),
        ("relogin_on_auth_error", "KEYWORD_ONLY", "False"),
    ),
    "get_token": (),
    "web_login": (
        ("url_to_redirect", "KEYWORD_ONLY", "'/'"),
        ("user_agent", "KEYWORD_ONLY", "None"),
    ),
    "get_me": (),
    "get_connects": (),
    "get_user": (("user_id", "POSITIONAL_OR_KEYWORD", "<required>"),),
    "get_projects": (
        ("categories_ids", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("price_from", "POSITIONAL_OR_KEYWORD", "None"),
        ("price_to", "POSITIONAL_OR_KEYWORD", "None"),
        ("hiring_from", "POSITIONAL_OR_KEYWORD", "None"),
        ("kworks_filter_from", "POSITIONAL_OR_KEYWORD", "None"),
        ("kworks_filter_to", "POSITIONAL_OR_KEYWORD", "None"),
        ("page", "POSITIONAL_OR_KEYWORD", "None"),
        ("query", "POSITIONAL_OR_KEYWORD", "None"),
    ),
    "get_dialogs_page": (
        ("page", "POSITIONAL_OR_KEYWORD", "1"),
        ("excluded_ids", "POSITIONAL_OR_KEYWORD", "None"),
    ),
    "get_dialog_with_user_page": (
        ("username", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("page", "KEYWORD_ONLY", "1"),
    ),
    "send_message": (
        ("user_id", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("text", "POSITIONAL_OR_KEYWORD", "<required>"),
    ),
    "delete_message": (("message_id", "POSITIONAL_OR_KEYWORD", "<required>"),),
    "get_categories": (),
    "get_notifications": (),
    "get_worker_orders": (),
    "request": (
        ("method", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("endpoint", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("use_token", "POSITIONAL_OR_KEYWORD", "False"),
        ("_headers", "POSITIONAL_OR_KEYWORD", "None"),
        ("_cookies", "POSITIONAL_OR_KEYWORD", "None"),
        ("retry", "KEYWORD_ONLY", "None"),
        ("timeout", "KEYWORD_ONLY", "None"),
        ("max_attempts", "KEYWORD_ONLY", "None"),
        ("**params", "VAR_KEYWORD", "<required>"),
    ),
    "request_with_body": (
        ("endpoint", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("use_token", "POSITIONAL_OR_KEYWORD", "False"),
        ("_headers", "POSITIONAL_OR_KEYWORD", "None"),
        ("_cookies", "POSITIONAL_OR_KEYWORD", "None"),
        ("body", "POSITIONAL_OR_KEYWORD", "None"),
        ("retry", "KEYWORD_ONLY", "None"),
        ("timeout", "KEYWORD_ONLY", "None"),
        ("max_attempts", "KEYWORD_ONLY", "None"),
        ("**params", "VAR_KEYWORD", "<required>"),
    ),
    "_handle_json_payload": (
        ("resp", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("endpoint", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("method", "KEYWORD_ONLY", "<required>"),
        ("request_params", "KEYWORD_ONLY", "<required>"),
        ("request_body", "KEYWORD_ONLY", "<required>"),
    ),
    "_read_response_body": (("resp", "POSITIONAL_OR_KEYWORD", "<required>"),),
    "_truncate": (
        ("text", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("limit", "KEYWORD_ONLY", "2048"),
    ),
    "inbox_edit": (
        ("use_token", "KEYWORD_ONLY", "True"),
        ("body", "KEYWORD_ONLY", "None"),
        ("**params", "VAR_KEYWORD", "<required>"),
    ),
}

_EXPECTED_WEB_SIGNATURES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "_build_xhr_headers": (
        ("user_agent", "KEYWORD_ONLY", "None"),
        ("accept", "KEYWORD_ONLY", "None"),
        ("referer", "KEYWORD_ONLY", "None"),
    ),
    "_filtered_cookies": (("url", "POSITIONAL_OR_KEYWORD", "<required>"),),
    "_maybe_add_csrf_headers": (
        ("url", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("headers", "POSITIONAL_OR_KEYWORD", "<required>"),
    ),
    "_raise_on_web_error": (
        ("resp", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("where", "KEYWORD_ONLY", "<required>"),
    ),
    "login_via_mobile_web_auth_token": (
        ("url_to_redirect", "KEYWORD_ONLY", "'/'"),
        ("user_agent", "KEYWORD_ONLY", "None"),
        ("allow_redirects", "KEYWORD_ONLY", "True"),
        ("max_redirects", "KEYWORD_ONLY", "10"),
        ("timeout", "KEYWORD_ONLY", "None"),
    ),
    "open_new_offer_page": (
        ("project_id", "KEYWORD_ONLY", "<required>"),
        ("user_agent", "KEYWORD_ONLY", "None"),
    ),
    "request": (
        ("method", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("path_or_url", "POSITIONAL_OR_KEYWORD", "<required>"),
        ("params", "KEYWORD_ONLY", "None"),
        ("data", "KEYWORD_ONLY", "None"),
        ("json_data", "KEYWORD_ONLY", "None"),
        ("headers", "KEYWORD_ONLY", "None"),
        ("allow_redirects", "KEYWORD_ONLY", "True"),
        ("timeout", "KEYWORD_ONLY", "None"),
    ),
    "quick_faq_init": (
        ("referer", "KEYWORD_ONLY", "<required>"),
        ("user_agent", "KEYWORD_ONLY", "None"),
        ("page", "KEYWORD_ONLY", "'new_offer'"),
    ),
    "create_offer_draft": (
        ("project_id", "KEYWORD_ONLY", "<required>"),
        ("csrftoken", "KEYWORD_ONLY", "<required>"),
        ("draft_key", "KEYWORD_ONLY", "<required>"),
        ("message", "KEYWORD_ONLY", "''"),
        ("referer", "KEYWORD_ONLY", "<required>"),
        ("user_agent", "KEYWORD_ONLY", "None"),
    ),
    "check_is_template": (
        ("want_id", "KEYWORD_ONLY", "<required>"),
        ("description", "KEYWORD_ONLY", "<required>"),
        ("referer", "KEYWORD_ONLY", "<required>"),
        ("user_agent", "KEYWORD_ONLY", "None"),
    ),
    "create_exchange_offer": (
        ("want_id", "KEYWORD_ONLY", "<required>"),
        ("offer_type", "KEYWORD_ONLY", "'custom'"),
        ("description", "KEYWORD_ONLY", "<required>"),
        ("kwork_duration", "KEYWORD_ONLY", "<required>"),
        ("kwork_price", "KEYWORD_ONLY", "<required>"),
        ("kwork_name", "KEYWORD_ONLY", "<required>"),
        ("user_agent", "KEYWORD_ONLY", "None"),
        ("extra_headers", "KEYWORD_ONLY", "None"),
        ("raise_on_error", "KEYWORD_ONLY", "True"),
        ("referer", "KEYWORD_ONLY", "None"),
    ),
}

_GENERIC_METHODS = {
    "projects",
    "project",
    "user_by_username",
    "user_search",
    "dialogs",
    "inboxes",
    "inbox_read",
    "favorite_categories",
    "offers",
    "offer",
    "delete_offer",
    "worker_orders",
    "get_order_details",
    "send_order_for_approval",
    "kworks_status_list",
    "user_kworks",
    "get_kwork_details_extra",
    "start_kwork",
    "pause_kwork",
    "exchange_info",
}


def _semantic_signature(function: Callable[..., Any], *, omit_self: bool = True) -> tuple[tuple[str, str, str], ...]:
    parameters: list[tuple[str, str, str]] = []
    for parameter in inspect.signature(function).parameters.values():
        if omit_self and parameter.name == "self":
            continue
        if parameter.name == "params" and parameter.kind is inspect.Parameter.VAR_KEYWORD:
            parameters.append(("**params", parameter.kind.name, "<required>"))
            continue
        default = "<required>" if parameter.default is inspect.Parameter.empty else repr(parameter.default)
        parameters.append((parameter.name, parameter.kind.name, default))
    return tuple(parameters)


def _generic_signature_is_valid(function: Callable[..., Any]) -> bool:
    parameters = _semantic_signature(function)
    return (
        len(parameters) == 2
        and parameters[0][0] == "use_token"
        and parameters[0][1] == "KEYWORD_ONLY"
        and parameters[1] == ("**params", "VAR_KEYWORD", "<required>")
    )


def _contract_snapshot() -> dict[str, Any]:
    signatures: dict[str, Any] = {}
    signatures["__init__"] = _semantic_signature(Kwork, omit_self=False)
    for name in sorted(set(_EXPECTED_SIGNATURES) - {"__init__"}):
        signatures[name] = _semantic_signature(getattr(Kwork, name))
    for name in sorted(_GENERIC_METHODS):
        signatures[name] = _semantic_signature(getattr(Kwork, name))
    web_signatures = {
        name: _semantic_signature(getattr(KworkWebClient, name)) for name in sorted(_EXPECTED_WEB_SIGNATURES)
    }
    return {
        "version": importlib.metadata.version(UPSTREAM_DISTRIBUTION),
        "signatures": signatures,
        "web_signatures": web_signatures,
        "routes": {key: sorted(value) for key, value in sorted(ROUTE_PARAMS.items())},
        "worker_order_statuses": WORKER_ORDER_STATUSES,
        "model_fields": {
            "Actor": sorted(Actor.model_fields),
            "Connects": sorted(Connects.model_fields),
            "DialogMessage": sorted(DialogMessage.model_fields),
            "InboxMessage": sorted(InboxMessage.model_fields),
            "User": sorted(User.model_fields),
            "WantWorker": sorted(WantWorker.model_fields),
        },
    }


def signature_fingerprint() -> str:
    encoded = json.dumps(
        _contract_snapshot(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_upstream_contract() -> ContractStatusData:
    try:
        version = importlib.metadata.version(UPSTREAM_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError as exc:
        raise ContractDriftError("kwork_distribution_missing") from exc
    if version != UPSTREAM_VERSION:
        raise ContractDriftError(f"kwork_version:{version}")

    for name, expected in _EXPECTED_SIGNATURES.items():
        target = Kwork if name == "__init__" else getattr(Kwork, name, None)
        if target is None:
            raise ContractDriftError(f"missing_method:{name}")
        actual = _semantic_signature(target, omit_self=name != "__init__")
        if actual != expected:
            raise ContractDriftError(f"signature:{name}:{actual!r}")

    for name in _GENERIC_METHODS:
        target = getattr(Kwork, name, None)
        if target is None or not _generic_signature_is_valid(target):
            raise ContractDriftError(f"generic_signature:{name}")

    for name, expected in _EXPECTED_WEB_SIGNATURES.items():
        target = getattr(KworkWebClient, name, None)
        if target is None or _semantic_signature(target) != expected:
            raise ContractDriftError(f"web_signature:{name}")

    required_actor_fields = {"id", "username"}
    required_project_fields = {"id", "title", "description", "date_confirm"}
    if not required_actor_fields.issubset(Actor.model_fields):
        raise ContractDriftError("actor_model_fields")
    if not required_project_fields.issubset(WantWorker.model_fields):
        raise ContractDriftError("project_model_fields")
    probe_client = Kwork("", "", retry_max_attempts=1)
    required_client_attributes = {
        "_token",
        "_read_response_body",
        "_truncate",
        "_normalize_timeout",
        "_web_client",
    }
    if any(not hasattr(probe_client, name) for name in required_client_attributes):
        raise ContractDriftError("private_client_dependencies")
    web_client = KworkWebClient(probe_client)
    if web_client._api is not probe_client or not web_client.base_url.startswith("https://kwork.ru/"):
        raise ContractDriftError("private_web_client_dependencies")
    actual_fingerprint = signature_fingerprint()
    if actual_fingerprint != EXPECTED_CONTRACT_FINGERPRINT:
        raise ContractDriftError(f"fingerprint:{actual_fingerprint}")

    return ContractStatusData(
        distribution=UPSTREAM_DISTRIBUTION,
        version=UPSTREAM_VERSION,
        signature_fingerprint=actual_fingerprint,
        verified=True,
    )
